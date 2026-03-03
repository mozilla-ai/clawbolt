"""Tool registry for decoupled tool registration.

Tool modules self-register with the default registry at import time.
The router calls ``create_tools(context)`` instead of manually importing
and assembling tools from every module.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.media.download import DownloadedMedia
from backend.app.models import Contractor
from backend.app.services.messaging import MessagingService
from backend.app.services.storage_service import StorageBackend

logger = logging.getLogger(__name__)

# Factory names that are always included regardless of message content.
_ALWAYS_INCLUDE: frozenset[str] = frozenset({"memory", "messaging", "profile"})

# Keyword patterns that trigger inclusion of specific tool factories.
# Each entry maps a factory name to a compiled regex of trigger words.
_KEYWORD_RULES: dict[str, re.Pattern[str]] = {
    "estimate": re.compile(
        r"\b(estimates?|quotes?|bids?|prices?|pricing|costs?|costing|invoices?|how\s+much)\b",
        re.IGNORECASE,
    ),
    "checklist": re.compile(
        r"\b(checklists?|reminders?|todos?|tasks?|to-dos?)\b",
        re.IGNORECASE,
    ),
}


@dataclass
class ToolContext:
    """Shared context passed to tool factories during creation."""

    db: Session
    contractor: Contractor
    storage: StorageBackend | None = None
    messaging_service: MessagingService | None = None
    to_address: str = ""
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)


@dataclass
class ToolFactory:
    """Metadata for a registered tool factory."""

    create: Callable[[ToolContext], list[Tool]]
    requires_storage: bool = False
    requires_messaging: bool = False


def select_tools(
    message: str,
    *,
    has_media: bool = False,
    has_storage: bool = False,
    factory_names: list[str] | None = None,
) -> set[str]:
    """Select which tool factories to include based on message context.

    Returns a set of factory names that should be activated for the given
    message. The selection logic is:

    - Always include: memory, messaging, profile tools
    - Include estimate tools when: message mentions pricing keywords
    - Include file tools when: media is present AND storage is configured
    - Include checklist tools when: message mentions task/checklist keywords
    - Fallback: when no specialized keywords match, include all tools

    Args:
        message: The inbound message text (may be empty).
        has_media: Whether the message includes media attachments.
        has_storage: Whether a storage backend is configured.
        factory_names: Available factory names. When ``None``, uses the
            default set of known factories.

    Returns:
        Set of factory names to include.
    """
    all_names = (
        set(factory_names)
        if factory_names is not None
        else {
            "memory",
            "messaging",
            "estimate",
            "checklist",
            "profile",
            "file",
        }
    )

    selected: set[str] = set(_ALWAYS_INCLUDE & all_names)

    # Check keyword rules for specialized tools
    specialized_matched = False
    for name, pattern in _KEYWORD_RULES.items():
        if name in all_names and pattern.search(message):
            selected.add(name)
            specialized_matched = True

    # Include file tools when media is present and storage is available.
    # Media presence is orthogonal to keyword matching: it adds file tools
    # but does not count as a "specialized match" for fallback purposes.
    if has_media and has_storage and "file" in all_names:
        selected.add("file")

    # Fallback: when no specialized keywords matched, include everything
    # so the model has full capability for ambiguous or general messages
    if not specialized_matched:
        selected = all_names.copy()

    return selected


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
        requires_messaging: bool = False,
    ) -> None:
        """Register a tool factory by name."""
        if name in self._factories:
            logger.warning("Overwriting existing tool factory: %s", name)
        self._factories[name] = ToolFactory(
            create=create,
            requires_storage=requires_storage,
            requires_messaging=requires_messaging,
        )

    def create_tools(
        self,
        context: ToolContext,
        *,
        selected_factories: set[str] | None = None,
    ) -> list[Tool]:
        """Create tools whose dependencies are satisfied by the context.

        When *selected_factories* is provided, only factories in that set are
        considered. Otherwise all registered factories are eligible.

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
            if factory.requires_messaging and context.messaging_service is None:
                logger.debug("Skipping %s: no messaging service", name)
                continue
            created = factory.create(context)
            for tool in created:
                if tool.params_model is None:
                    raise ValueError(
                        f"Tool '{tool.name}' from factory '{name}' is missing "
                        f"a params_model. All tools must define a Pydantic "
                        f"BaseModel for parameter validation."
                    )
            tools.extend(created)
        return tools

    @property
    def factory_names(self) -> list[str]:
        """Return sorted list of registered factory names."""
        return sorted(self._factories)


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
