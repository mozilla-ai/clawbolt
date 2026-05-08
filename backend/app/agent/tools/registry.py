"""Tool registry for decoupled tool registration.

Tool modules self-register with the default registry at import time.
The router calls ``create_tools(context)`` instead of manually importing
and assembling tools from every module.

Factories are classified as **core** (always-available) or **specialist**
(per-integration, listed via the ``list_capabilities`` meta-tool). Both
tiers are loaded on every message for a given user when their
dependencies are met: core unconditionally, specialists when the user is
authenticated for the underlying integration (see
``create_ready_specialist_tools``). The Anthropic prompt-cache key
includes the tools block, so the per-message tool list is held stable as
a function of the user's auth state and dashboard config; varying it per
message busts the cached system prompt prefix (issue #1170).

The ``list_capabilities`` meta-tool is a discovery hint, not an
activation mechanism: it tells the LLM which integrations the user could
connect, so the agent can prompt the user to authenticate. Tools for
authenticated integrations are already on the schema from turn 1 and do
not need a runtime activation round trip.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
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
    # Current turn's message text, used by media tools so analyze_photo can
    # fall back to the caption when the agent doesn't pass an explicit context.
    turn_text: str = ""


@dataclass
class SubToolInfo:
    """Static metadata for an individual tool within a factory."""

    name: str
    description: str
    default_permission: str = "always"
    # When True, this sub-tool is omitted from the dashboard Permissions
    # page. The tool still runs normally and still respects stored
    # permission overrides -- the flag only hides UI chrome for tools the
    # user shouldn't have to think about (e.g. send_media_reply, whose
    # default level should always be ALWAYS because it's the agent's
    # media delivery path).
    hidden_in_permissions: bool = False

    def __post_init__(self) -> None:
        PermissionLevel(self.default_permission)  # validates at registration time


@dataclass
class ToolFactory:
    """Metadata for a registered tool factory."""

    create: Callable[[ToolContext], list[Tool]] | Callable[[ToolContext], Awaitable[list[Tool]]]
    requires_storage: bool = False
    requires_outbound: bool = False
    core: bool = True
    summary: str = ""
    sub_tools: list[SubToolInfo] = field(default_factory=list)
    auth_check: Callable[[ToolContext], Awaitable[str | None]] | None = None


class ListCapabilitiesParams(BaseModel):
    """Parameters for the list_capabilities meta-tool."""

    category: str | None = Field(
        default=None,
        description=(
            "Category name to look up usage guidance for. Omit to see all "
            "available categories and connection status."
        ),
    )


def create_list_capabilities_tool(
    specialist_summaries: dict[str, str],
    unauthenticated: dict[str, str] | None = None,
    disabled_sub_tools: dict[str, list[SubToolInfo]] | None = None,
) -> Tool:
    """Create the ``list_capabilities`` meta-tool.

    Discovery and documentation lookup for specialist tool categories.
    Tools for authenticated integrations are already loaded on the
    schema; this tool exists to surface unconnected integrations and
    deliver SKILL.md usage guidance on demand.

    *unauthenticated* maps category names to human-readable reasons why
    the integration is not yet connected (e.g. missing OAuth). These
    categories are listed but their tools are not loaded.

    *disabled_sub_tools* maps specialist factory names to lists of
    ``SubToolInfo`` for individual tools the user has disabled. This
    information is surfaced so the LLM can tell users about disabled
    capabilities.
    """
    from backend.app.agent.skills.loader import get_skill_instructions

    _unauthenticated = unauthenticated or {}
    _disabled_subs = disabled_sub_tools or {}

    async def list_capabilities(category: str | None = None) -> ToolResult:
        if category is None:
            if not specialist_summaries and not _unauthenticated:
                return ToolResult(content="No additional capabilities available.")
            lines: list[str] = []
            if specialist_summaries:
                lines.append(
                    "Connected specialist capabilities "
                    "(tools are already loaded; call list_capabilities with a "
                    "category name for usage guidance):"
                )
                for name, summary in sorted(specialist_summaries.items()):
                    disabled_for_cat = _disabled_subs.get(name, [])
                    if disabled_for_cat:
                        disabled_names = ", ".join(st.name for st in disabled_for_cat)
                        lines.append(f"- {name}: {summary} [disabled: {disabled_names}]")
                    else:
                        lines.append(f"- {name}: {summary}")
            if _unauthenticated:
                lines.append("")
                lines.append("Not connected (user must authenticate before use):")
                for name, reason in sorted(_unauthenticated.items()):
                    lines.append(f"- {name}: {reason}")
            return ToolResult(content="\n".join(lines))

        if category in _unauthenticated:
            return ToolResult(
                content=_unauthenticated[category],
                is_error=True,
                error_kind=ToolErrorKind.AUTH,
            )

        if category not in specialist_summaries:
            available = ", ".join(sorted(specialist_summaries.keys()))
            return ToolResult(
                content=f'Unknown category "{category}". Available: {available}',
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        guidance_msg = (
            f'Tools for "{category}" are already loaded and callable. '
            "Call the specific tool to perform the user's request."
        )
        disabled_for_cat = _disabled_subs.get(category, [])
        if disabled_for_cat:
            disabled_names = ", ".join(st.name for st in disabled_for_cat)
            guidance_msg += (
                f"\nNote: the following tools in this category are disabled by the user "
                f"and will not be available: {disabled_names}. "
                "The user can re-enable them in Settings."
            )
        skill_instructions = get_skill_instructions(category)
        if skill_instructions:
            guidance_msg += f"\n\n{skill_instructions}"
        return ToolResult(content=guidance_msg)

    summary_lines = [
        f"  - {name}: {summary}" for name, summary in sorted(specialist_summaries.items())
    ]
    summary_block = "\n".join(summary_lines)
    unauth_hint = ""
    if _unauthenticated:
        unauth_lines = [f"  - {name} (not connected)" for name in sorted(_unauthenticated)]
        unauth_hint = (
            "\nThe following integrations are configured but not yet connected:\n"
            + "\n".join(unauth_lines)
            + "\nIf the user asks about them, let them know they need to "
            "connect the integration first."
        )
    disabled_hint = ""
    if _disabled_subs:
        disabled_hint = (
            "\nSome tools are disabled by the user. If a user asks about a "
            "capability that seems related to an available category, check if "
            "it might be a disabled tool and let them know they can re-enable it."
        )
    return Tool(
        name=ToolName.LIST_CAPABILITIES,
        description=(
            "Discover specialist capabilities and look up usage guidance. "
            "Call without arguments to see connected and unconnected categories. "
            "Call with a category name for detailed usage guidance for that "
            "category's already-loaded tools."
        ),
        function=list_capabilities,
        params_model=ListCapabilitiesParams,
        usage_hint=(
            "You have specialist capabilities (tools already loaded):\n"
            f"{summary_block}\n"
            "Call list_capabilities with a category name when you need usage "
            "guidance for that category's tools."
            f"{unauth_hint}"
            f"{disabled_hint}"
        ),
    )


class ToolRegistry:
    """Registry that collects tool factories and creates tools from context."""

    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(
        self,
        name: str,
        create: Callable[[ToolContext], list[Tool]]
        | Callable[[ToolContext], Awaitable[list[Tool]]],
        *,
        requires_storage: bool = False,
        requires_outbound: bool = False,
        core: bool = True,
        summary: str = "",
        sub_tools: list[SubToolInfo] | None = None,
        auth_check: Callable[[ToolContext], Awaitable[str | None]] | None = None,
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
            auth_check: Optional callable that checks whether the user has
                authenticated for this integration. Returns ``None`` when
                ready, or a human-readable reason string when auth is
                missing. Used to surface unauthenticated integrations to
                the LLM so it knows not to attempt activation.
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
            auth_check=auth_check,
        )

    async def create_tools(
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

        Factories are iterated in sorted factory-name order so the resulting
        tool schema sequence is deterministic across process restarts. The
        Anthropic tools cache key is order-sensitive, so a stable prefix
        depends on stable ordering regardless of module import order.

        Factories may be sync or async callables.
        """
        tools: list[Tool] = []
        for name in sorted(self._factories):
            factory = self._factories[name]
            if selected_factories is not None and name not in selected_factories:
                logger.debug("Skipping %s: not selected for this message", name)
                continue
            if factory.requires_storage and context.storage is None:
                logger.debug("Skipping %s: no storage backend", name)
                continue
            if factory.requires_outbound and context.publish_outbound is None:
                logger.debug("Skipping %s: no publish_outbound callback", name)
                continue
            result = factory.create(context)
            created: list[Tool] = await result if inspect.isawaitable(result) else result  # type: ignore[assignment]
            if excluded_tool_names:
                created = [t for t in created if t.name not in excluded_tool_names]
            # Auto-attach an ApprovalPolicy to any SubToolInfo-registered tool
            # that lacks one. This makes the user's stored permission overrides
            # authoritative for every user-controllable tool: even tools whose
            # SubToolInfo default is "always" can be escalated to "ask" via the
            # dashboard, and the runtime gate at core.py will respect that.
            # Tools that are deliberately not user-controllable (the meta tool,
            # heartbeat-context overrides) have no SubToolInfo and are left
            # alone, preserving the policy=None hard-bypass semantic.
            if factory.sub_tools:
                sub_by_name = {st.name: st for st in factory.sub_tools}
                for tool in created:
                    if tool.approval_policy is None and tool.name in sub_by_name:
                        sub = sub_by_name[tool.name]
                        tool.approval_policy = ApprovalPolicy(
                            default_level=PermissionLevel(sub.default_permission),
                            description_builder=lambda _args, _d=sub.description: _d,
                        )
            tools.extend(created)
        return tools

    async def create_core_tools(
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
        return await self.create_tools(
            context, selected_factories=selected, excluded_tool_names=excluded_tool_names
        )

    async def get_available_specialist_summaries(
        self,
        context: ToolContext,
        *,
        excluded_factories: set[str] | None = None,
    ) -> dict[str, str]:
        """Return summaries of specialist factories whose dependencies are met.

        Used by the setup code to build the ``list_capabilities`` meta-tool
        with only the categories that are actually usable.

        Factories that have an ``auth_check`` returning a non-None value
        (i.e. user has not authenticated) are excluded here. Use
        ``get_unauthenticated_specialists`` to retrieve those separately.

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
            if factory.auth_check is not None and await factory.auth_check(context) is not None:
                continue
            summaries[name] = factory.summary
        return summaries

    async def create_ready_specialist_tools(
        self,
        context: ToolContext,
        *,
        excluded_factories: set[str] | None = None,
        excluded_tool_names: set[str] | None = None,
    ) -> list[Tool]:
        """Materialize specialist tools for categories the user is ready to use.

        A specialist is "ready" when:
        - it is not excluded,
        - its infrastructure deps (storage, outbound) are met,
        - its ``auth_check`` is None OR returns None (user authenticated).

        Loaded onto the schema from turn 1 so the LLM can call them
        directly without a discovery round trip.
        """
        ready: set[str] = set()
        for name, factory in self._factories.items():
            if factory.core:
                continue
            if excluded_factories and name in excluded_factories:
                continue
            if factory.requires_storage and context.storage is None:
                continue
            if factory.requires_outbound and context.publish_outbound is None:
                continue
            if factory.auth_check is not None and await factory.auth_check(context) is not None:
                continue
            ready.add(name)
        if not ready:
            return []
        return await self.create_tools(
            context,
            selected_factories=ready,
            excluded_tool_names=excluded_tool_names,
        )

    async def get_unauthenticated_specialists(
        self,
        context: ToolContext,
        *,
        excluded_factories: set[str] | None = None,
    ) -> dict[str, str]:
        """Return specialist factories that are configured but not authenticated.

        Returns a mapping of ``{factory_name: reason}`` for specialists whose
        ``auth_check`` returns a non-None reason string. Factories without an
        ``auth_check`` or whose infrastructure dependencies (storage, outbound)
        are unmet are excluded.
        """
        unauthenticated: dict[str, str] = {}
        for name, factory in self._factories.items():
            if factory.core:
                continue
            if excluded_factories and name in excluded_factories:
                continue
            if factory.requires_storage and context.storage is None:
                continue
            if factory.requires_outbound and context.publish_outbound is None:
                continue
            if factory.auth_check is None:
                continue
            reason = await factory.auth_check(context)
            if reason is not None:
                unauthenticated[name] = reason
        return unauthenticated

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

    def get_disabled_specialist_sub_tools(
        self,
        disabled_sub_tool_names: set[str],
    ) -> dict[str, list[SubToolInfo]]:
        """Map specialist factory names to their disabled sub-tools.

        Given a flat set of disabled sub-tool names (from ``ToolConfigStore``),
        returns only specialist factories that have at least one disabled
        sub-tool, mapped to the list of those disabled ``SubToolInfo`` objects.
        """
        if not disabled_sub_tool_names:
            return {}
        result: dict[str, list[SubToolInfo]] = {}
        for name, factory in self._factories.items():
            if factory.core:
                continue
            disabled = [st for st in factory.sub_tools if st.name in disabled_sub_tool_names]
            if disabled:
                result[name] = disabled
        return result

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


_tool_modules_imported = False


def ensure_tool_modules_imported() -> None:
    """Auto-discover and import all tool modules.

    Scans two locations:

    1. ``backend.app.agent.tools.*_tools`` -- core tool modules
    2. ``backend.app.integrations.*.factory`` -- integration packages

    Guarded so the discovery loop and its log messages only run once,
    even when called from multiple import sites.
    """
    global _tool_modules_imported
    if _tool_modules_imported:
        return
    _tool_modules_imported = True

    # 1. Core tools: modules ending with _tools in backend/app/agent/tools/
    package = importlib.import_module("backend.app.agent.tools")
    for _, name, _ in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if name.endswith("_tools"):
            try:
                importlib.import_module(name)
                logger.debug("Imported tool module: %s", name)
            except Exception:
                logger.exception("Failed to import tool module: %s", name)

    # 2. Integration packages: factory module in each backend/app/integrations/*/
    try:
        integrations_pkg = importlib.import_module("backend.app.integrations")
        for _, pkg_name, is_pkg in pkgutil.iter_modules(
            integrations_pkg.__path__, integrations_pkg.__name__ + "."
        ):
            if not is_pkg:
                continue
            factory_module = f"{pkg_name}.factory"
            try:
                importlib.import_module(factory_module)
                logger.debug("Imported integration module: %s", factory_module)
            except ModuleNotFoundError:
                logger.debug("No factory module in %s, skipping", pkg_name)
            except Exception:
                logger.exception("Failed to import integration module: %s", factory_module)
    except ModuleNotFoundError:
        logger.debug("No integrations package found, skipping integration discovery")

    logger.info(
        "Tool registry: %d factories registered: %s",
        len(default_registry.factory_names),
        ", ".join(sorted(default_registry.factory_names)),
    )
