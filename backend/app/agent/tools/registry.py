"""Tool registry for decoupled tool registration.

Tool modules self-register with the default registry at import time.
The router calls ``create_tools(context)`` instead of manually importing
and assembling tools from every module.

Factories are classified as **core** (always-available) or **specialist**
(discovered on demand via the ``list_capabilities`` meta-tool).  Core tools
are registered to the LLM on every message; specialist tools require the
agent to explicitly activate them, keeping the initial schema payload small
and enabling progressive disclosure as tool count grows.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.media.download import DownloadedMedia
from backend.app.models import User
from backend.app.services.storage_service import StorageBackend

if TYPE_CHECKING:
    from backend.app.bus import OutboundMessage

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Shared context passed to tool factories during creation."""

    user: User
    storage: StorageBackend | None = None
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]] | None = None
    channel: str = ""
    to_address: str = ""
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)


@dataclass
class SubToolInfo:
    """Static metadata for an individual tool within a factory."""

    name: str
    description: str


@dataclass
class ToolFactory:
    """Metadata for a registered tool factory."""

    create: Callable[[ToolContext], list[Tool]]
    requires_storage: bool = False
    requires_outbound: bool = False
    core: bool = True
    summary: str = ""
    sub_tools: list[SubToolInfo] = field(default_factory=list)


class ListCapabilitiesParams(BaseModel):
    """Parameters for the list_capabilities meta-tool."""

    category: str | None = Field(
        default=None,
        description="Category name to activate. Omit to see all available categories.",
    )


def create_list_capabilities_tool(
    specialist_summaries: dict[str, str],
) -> Tool:
    """Create the ``list_capabilities`` meta-tool.

    The tool itself only returns text describing available specialist
    categories.  Actual tool schema injection is handled by the agent
    loop in ``core.py`` after detecting a ``list_capabilities`` call.

    When a category is activated that has an associated SKILL.md, the
    skill instructions are included in the response so the LLM has
    usage guidance alongside the new tool schemas.
    """
    from backend.app.agent.skills.loader import get_skill_instructions

    async def list_capabilities(category: str | None = None) -> ToolResult:
        if category is None:
            if not specialist_summaries:
                return ToolResult(content="No additional capabilities available.")
            lines = [
                "Available specialist capabilities "
                "(call list_capabilities with a category name to activate):"
            ]
            for name, summary in sorted(specialist_summaries.items()):
                lines.append(f"- {name}: {summary}")
            return ToolResult(content="\n".join(lines))

        if category not in specialist_summaries:
            available = ", ".join(sorted(specialist_summaries.keys()))
            return ToolResult(
                content=f'Unknown category "{category}". Available: {available}',
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        activation_msg = (
            f'Category "{category}" activated. Tools are available in your next response.'
        )
        skill_instructions = get_skill_instructions(category)
        if skill_instructions:
            activation_msg += f"\n\n{skill_instructions}"
        return ToolResult(content=activation_msg)

    summary_lines = [
        f"  - {name}: {summary}"
        for name, summary in sorted(specialist_summaries.items())
    ]
    summary_block = "\n".join(summary_lines)
    return Tool(
        name=ToolName.LIST_CAPABILITIES,
        description=(
            "Discover and activate specialist tool capabilities. "
            "Call without arguments to see available categories. "
            "Call with a category name to activate those tools."
        ),
        function=list_capabilities,
        params_model=ListCapabilitiesParams,
        usage_hint=(
            "You have specialist capabilities:\n"
            f"{summary_block}\n"
            "Call list_capabilities with a category name to activate the tools "
            "before using them. Activate proactively when the user's message "
            "relates to a specialist category."
        ),
    )


class ToolRegistry:
    """Registry that collects tool factories and creates tools from context."""

    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(
        self,
        name: str,
        create: Callable[[ToolContext], list[Tool]],
        *,
        requires_storage: bool = False,
        requires_outbound: bool = False,
        core: bool = True,
        summary: str = "",
        sub_tools: list[SubToolInfo] | None = None,
    ) -> None:
        """Register a tool factory by name.

        Args:
            name: Unique factory name.
            create: Callable that produces a list of ``Tool`` objects.
            requires_storage: Skip this factory when no storage backend exists.
            requires_outbound: Skip this factory when no publish_outbound callback exists.
            core: If ``True`` the factory's tools are always registered.
                If ``False`` the factory is a specialist, discoverable via
                ``list_capabilities``.
            summary: One-line description shown by ``list_capabilities`` for
                specialist factories.
            sub_tools: Static metadata for individual tools this factory creates.
        """
        if name in self._factories:
            logger.warning("Overwriting existing tool factory: %s", name)
        self._factories[name] = ToolFactory(
            create=create,
            requires_storage=requires_storage,
            requires_outbound=requires_outbound,
            core=core,
            summary=summary,
            sub_tools=sub_tools or [],
        )

    def create_tools(
        self,
        context: ToolContext,
        *,
        selected_factories: set[str] | None = None,
        excluded_tool_names: set[str] | None = None,
    ) -> list[Tool]:
        """Create tools whose dependencies are satisfied by the context.

        When *selected_factories* is provided, only factories in that set are
        considered. Otherwise all registered factories are eligible.

        When *excluded_tool_names* is provided, individual tools whose names
        appear in the set are filtered out after creation.

        Every tool must have a ``params_model`` set so that Pydantic
        validation runs on all arguments before execution.
        """
        tools: list[Tool] = []
        for name, factory in self._factories.items():
            if selected_factories is not None and name not in selected_factories:
                logger.debug("Skipping %s: not selected for this message", name)
                continue
            if factory.requires_storage and context.storage is None:
                logger.debug("Skipping %s: no storage backend", name)
                continue
            if factory.requires_outbound and context.publish_outbound is None:
                logger.debug("Skipping %s: no publish_outbound callback", name)
                continue
            created = factory.create(context)
            if excluded_tool_names:
                created = [t for t in created if t.name not in excluded_tool_names]
            tools.extend(created)
        return tools

    def create_core_tools(
        self,
        context: ToolContext,
        *,
        excluded_factories: set[str] | None = None,
        excluded_tool_names: set[str] | None = None,
    ) -> list[Tool]:
        """Create only core (always-available) tools.

        When *excluded_factories* is provided, factories in that set are
        skipped even if they are core factories.

        When *excluded_tool_names* is provided, individual tools whose names
        appear in the set are filtered out after creation.
        """
        selected = self.core_factory_names
        if excluded_factories:
            selected = selected - excluded_factories
        return self.create_tools(
            context, selected_factories=selected, excluded_tool_names=excluded_tool_names
        )

    def get_available_specialist_summaries(
        self,
        context: ToolContext,
        *,
        excluded_factories: set[str] | None = None,
    ) -> dict[str, str]:
        """Return summaries of specialist factories whose dependencies are met.

        Used by the setup code to build the ``list_capabilities`` meta-tool
        with only the categories that are actually usable.

        When *excluded_factories* is provided, factories in that set are
        skipped.
        """
        summaries: dict[str, str] = {}
        for name, factory in self._factories.items():
            if factory.core:
                continue
            if excluded_factories and name in excluded_factories:
                continue
            if factory.requires_storage and context.storage is None:
                continue
            if factory.requires_outbound and context.publish_outbound is None:
                continue
            summaries[name] = factory.summary
        return summaries

    @property
    def core_factory_names(self) -> set[str]:
        """Return the set of core factory names."""
        return {name for name, f in self._factories.items() if f.core}

    @property
    def specialist_factory_names(self) -> set[str]:
        """Return the set of specialist factory names."""
        return {name for name, f in self._factories.items() if not f.core}

    @property
    def factory_names(self) -> list[str]:
        """Return sorted list of registered factory names."""
        return sorted(self._factories)

    def get_factory_sub_tools(self, factory_name: str) -> list[SubToolInfo]:
        """Return sub-tool metadata for a factory, or empty list if unknown."""
        factory = self._factories.get(factory_name)
        return factory.sub_tools if factory else []

    @property
    def specialist_summaries(self) -> dict[str, str]:
        """Return summaries of all specialist factories.

        Unlike ``get_available_specialist_summaries`` this does not require
        a ``ToolContext`` and does not filter by dependency availability.
        Useful for prompt building where the full capability list is wanted.
        """
        return {name: f.summary for name, f in self._factories.items() if not f.core and f.summary}


# Module-level singleton used by tool modules for self-registration.
default_registry = ToolRegistry()


def ensure_tool_modules_imported() -> None:
    """Auto-discover and import all tool modules that end with ``_tools``.

    This is idempotent: Python's import system caches modules, so repeated
    calls are essentially free.
    """
    package = importlib.import_module("backend.app.agent.tools")
    for _, name, _ in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if name.endswith("_tools"):
            importlib.import_module(name)
