"""Database-backed memory store.

Replaces FileMemoryStore from file_store.py. Uses MemoryDocument ORM model
for MEMORY.md and HISTORY.md content, and User ORM model for soul_text and
user_text.
"""

from __future__ import annotations

import logging

from backend.app.agent.store_cache import StoreCache
from backend.app.database import SessionLocal, db_session
from backend.app.models import MemoryDocument, User

logger = logging.getLogger(__name__)


class MemoryStore:
    """Database-backed memory storage using MemoryDocument ORM model."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def _get_or_create_doc(self, db: object) -> MemoryDocument:
        """Get or create the MemoryDocument row for this user."""
        from sqlalchemy.orm import Session as SASession

        assert isinstance(db, SASession)
        doc = db.query(MemoryDocument).filter_by(user_id=self.user_id).first()
        if doc is None:
            doc = MemoryDocument(user_id=self.user_id, memory_text="", history_text="")
            db.add(doc)
            db.flush()
        return doc

    def read_memory(self) -> str:
        """Read memory text (equivalent of MEMORY.md)."""
        db = SessionLocal()
        try:
            doc = db.query(MemoryDocument).filter_by(user_id=self.user_id).first()
            if doc is None:
                return ""
            return (doc.memory_text or "").strip()
        finally:
            db.close()

    def write_memory(self, content: str) -> None:
        """Write memory text (full rewrite, equivalent of MEMORY.md)."""
        with db_session() as db:
            doc = self._get_or_create_doc(db)
            doc.memory_text = content.rstrip() + "\n"
            db.commit()

    def read_history(self) -> str:
        """Read history text (equivalent of HISTORY.md)."""
        db = SessionLocal()
        try:
            doc = db.query(MemoryDocument).filter_by(user_id=self.user_id).first()
            if doc is None:
                return ""
            return (doc.history_text or "").strip()
        finally:
            db.close()

    async def append_history(self, entry: str) -> None:
        """Append an entry to history text (equivalent of HISTORY.md)."""
        from sqlalchemy import case as sa_case
        from sqlalchemy import literal_column

        with db_session() as db:
            doc = self._get_or_create_doc(db)
            # Use SQL-level concatenation to avoid lost-update races
            suffix = entry + "\n"
            db.query(MemoryDocument).filter_by(id=doc.id).update(
                {
                    MemoryDocument.history_text: sa_case(
                        (MemoryDocument.history_text.is_(None), literal_column("''")),
                        else_=MemoryDocument.history_text,
                    )
                    + suffix
                },
                synchronize_session="fetch",
            )
            db.commit()

    def read_soul(self) -> str:
        """Read soul text from User model."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is None:
                return ""
            raw = (user.soul_text or "").strip()
            if raw.startswith("# Soul"):
                raw = raw[len("# Soul") :].strip()
            return raw
        finally:
            db.close()

    def write_soul(self, content: str) -> None:
        """Write soul text to User model."""
        with db_session() as db:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is not None:
                user.soul_text = f"# Soul\n\n{content}\n"
                db.commit()

    def read_user(self) -> str:
        """Read user text from User model."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is None:
                return ""
            raw = (user.user_text or "").strip()
            if raw.startswith("# User"):
                raw = raw[len("# User") :].strip()
            return raw
        finally:
            db.close()

    def write_user(self, content: str) -> None:
        """Write user text to User model."""
        with db_session() as db:
            user = db.query(User).filter_by(id=self.user_id).first()
            if user is not None:
                user.user_text = f"# User\n\n{content}\n"
                db.commit()

    async def build_memory_context(self) -> str:
        """Build memory context for injection into the agent prompt."""
        return self.read_memory()


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

_cache: StoreCache[MemoryStore] = StoreCache(MemoryStore)


def get_memory_store(user_id: str) -> MemoryStore:
    """Get or create a MemoryStore for the given user.

    Uses an LRU cache bounded to 256 entries to prevent unbounded memory
    growth in multi-tenant deployments.
    """
    return _cache.get(user_id)


def reset_memory_stores() -> None:
    """Clear the memory store cache (for tests)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Module-level convenience functions (formerly in memory.py)
# ---------------------------------------------------------------------------


async def build_memory_context(user_id: str) -> str:
    """Build memory context text for injection into the agent prompt."""
    store = get_memory_store(user_id)
    return await store.build_memory_context()


def read_memory(user_id: str) -> str:
    """Read raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    return store.read_memory()


def write_memory(user_id: str, content: str) -> None:
    """Write raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    store.write_memory(content)
