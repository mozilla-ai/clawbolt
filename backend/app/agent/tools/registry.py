"""Tool registry for declarative tool registration.

Provides a ToolRegistry that tool modules register with, decoupling the
router from knowledge of individual tool modules and their dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.models import Contractor
from backend.app.services.messaging import MessagingService
from backend.app.services.storage_service import StorageBackend


@dataclass
class ToolContext:
    """Shared dependencies passed to tool factories."""

    db: Session
    contractor: Contractor
    storage: StorageBackend | None = None
    messaging_service: MessagingService | None = None
    to_address: str = ""
    downloaded_media: dict[str, bytes] = field(default_factory=dict)


@dataclass
class ToolFactory:
    """Declarative tool factory with dependency requirements."""

    create: Callable[[ToolContext], list[Tool]]
    requires_storage: bool = False
    requires_messaging: bool = False


class ToolRegistry:
    """Registry of tool factories.

    Tool modules register their factories at import time. The router
    creates a ToolContext and calls create_tools() to get all tools,
    without needing to know about individual tool modules.
    """

    def __init__(self) -> None:
        self._factories: list[ToolFactory] = []

    def register(self, factory: ToolFactory) -> None:
        """Register a tool factory."""
        self._factories.append(factory)

    def create_tools(self, context: ToolContext) -> list[Tool]:
        """Create all tools whose requirements are satisfied by the context."""
        tools: list[Tool] = []
        for factory in self._factories:
            if factory.requires_storage and not context.storage:
                continue
            if factory.requires_messaging and not context.messaging_service:
                continue
            tools.extend(factory.create(context))
        return tools


default_registry = ToolRegistry()

_tool_modules_imported = False


def ensure_tool_modules_imported() -> None:
    """Import all tool modules so they register with the default registry.

    Safe to call multiple times; only imports on the first call.
    """
    global _tool_modules_imported
    if _tool_modules_imported:
        return
    _tool_modules_imported = True

    import importlib

    for module_name in (
        "backend.app.agent.tools.memory_tools",
        "backend.app.agent.tools.messaging_tools",
        "backend.app.agent.tools.estimate_tools",
        "backend.app.agent.tools.checklist_tools",
        "backend.app.agent.tools.profile_tools",
        "backend.app.agent.tools.file_tools",
    ):
        importlib.import_module(module_name)
