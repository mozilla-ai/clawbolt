"""Database-backed memory store.

Replaces FileMemoryStore from file_store.py. Uses MemoryDocument ORM model
for MEMORY.md and HISTORY.md content, and User ORM model for soul_text and
user_text.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

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

_MAX_CACHED_STORES = 256
_stores: OrderedDict[str, MemoryStore] = OrderedDict()


def get_memory_store(user_id: str) -> MemoryStore:
    """Get or create a MemoryStore for the given user.

    Uses an LRU cache bounded to ``_MAX_CACHED_STORES`` entries to prevent
    unbounded memory growth in multi-tenant deployments.
    """
    if user_id in _stores:
        _stores.move_to_end(user_id)
        return _stores[user_id]
    store = MemoryStore(user_id)
    _stores[user_id] = store
    if len(_stores) > _MAX_CACHED_STORES:
        _stores.popitem(last=False)
    return store


def reset_memory_stores() -> None:
    """Clear the memory store cache (for tests)."""
    _stores.clear()
