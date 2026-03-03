"""Tool registry for decoupled tool registration.

Tool modules self-register with the default registry at import time.
The router calls ``create_tools(context)`` instead of manually importing
and assembling tools from every module.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool
from backend.app.media.download import DownloadedMedia
from backend.app.models import Contractor
from backend.app.services.messaging import MessagingService
from backend.app.services.storage_service import StorageBackend

logger = logging.getLogger(__name__)

# All tool modules that should be imported to trigger self-registration.
_TOOL_MODULES: list[str] = [
    "backend.app.agent.tools.memory_tools",
    "backend.app.agent.tools.messaging_tools",
    "backend.app.agent.tools.estimate_tools",
    "backend.app.agent.tools.checklist_tools",
    "backend.app.agent.tools.profile_tools",
    "backend.app.agent.tools.file_tools",
]


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

    def create_tools(self, context: ToolContext) -> list[Tool]:
        """Create all tools whose dependencies are satisfied by the context.

        Every tool must have a ``params_model`` set so that Pydantic
        validation runs on all arguments before execution.
        """
        tools: list[Tool] = []
        for name, factory in self._factories.items():
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
    """Import all tool modules so they self-register with ``default_registry``.

    This is idempotent: Python's import system caches modules, so repeated
    calls are essentially free.
    """
    for module_path in _TOOL_MODULES:
        importlib.import_module(module_path)
