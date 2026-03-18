"""Compatibility shim for the legacy file_store module.

All data classes, stores, and utilities have been migrated to dedicated modules:
- DTOs and utilities: backend.app.agent.dto
- User store: backend.app.agent.user_db
- Session store: backend.app.agent.session_db
- Memory store: backend.app.agent.memory_db
- Heartbeat/Media/Idempotency/LLM/ToolConfig stores: backend.app.agent.stores

This module re-exports everything for backward compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-export all DTOs
from backend.app.agent.dto import (  # noqa: F401
    HeartbeatLogEntry,
    MediaData,
    SessionMetadata,
    SessionState,
    StoredMessage,
    SubToolEntry,
    ToolConfigEntry,
    UserData,
    slugify,
)

# Re-export stores
from backend.app.agent.stores import (  # noqa: F401
    HeartbeatStore,
    IdempotencyStore,
    LLMUsageStore,
    MediaStore,
    ToolConfigStore,
    get_idempotency_store,
    reset_stores,
)

# Re-export user store
from backend.app.agent.user_db import UserStore, get_user_store  # noqa: F401

if TYPE_CHECKING:
    from backend.app.agent.memory_db import MemoryStore
    from backend.app.agent.session_db import SessionStore


# Re-export session/memory store factories for premium backward compat


def get_session_store(user_id: str | int) -> SessionStore:
    """Lazy import to avoid circular deps."""
    from backend.app.agent.session_db import SessionStore as _SessionStore

    return _SessionStore(str(user_id))


def get_memory_store(user_id: str | int) -> MemoryStore:
    """Lazy import to avoid circular deps."""
    from backend.app.agent.memory_db import MemoryStore as _MemoryStore

    return _MemoryStore(str(user_id))
