"""Database-backed memory store.

Replaces FileMemoryStore from file_store.py. Uses MemoryDocument ORM model
for MEMORY.md and HISTORY.md content, and User ORM model for soul_text and
user_text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import Select, Update, literal_column, select, update
from sqlalchemy import case as sa_case

from backend.app.agent.store_cache import StoreCache
from backend.app.database import (
    AsyncSessionLocal,
    SessionLocal,
    db_session,
    db_session_async,
)
from backend.app.models import MemoryDocument, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure typed builders shared by sync and async paths.
#
# Mirrors the IdempotencyStore pilot (issue #1150 / PR #1199): each
# public sync method has an ``*_async`` peer; both forward through the
# same ``select(...) / update(...)`` builders so the two paths cannot
# drift. Builders return concretely-typed SQLAlchemy core constructs;
# no ``Any`` so ``ty`` can verify call sites end-to-end.
# ---------------------------------------------------------------------------


def _doc_select(user_id: str) -> Select[tuple[MemoryDocument]]:
    """Builder shared by every read of the user's MemoryDocument row."""
    return select(MemoryDocument).filter_by(user_id=user_id)


def _user_select(user_id: str) -> Select[tuple[User]]:
    """Builder shared by every read of the user's User row."""
    return select(User).filter_by(id=user_id)


def _append_history_update(doc_id: int, entry: str) -> Update:
    """Build the SQL-level history-append used by both sync and async paths.

    Uses a CASE expression to substitute an empty string when the
    column is NULL so concatenation yields the expected text instead
    of NULL. Pulls the suffix into a single value so the SQL emitted
    matches what the original sync path produced.
    """
    suffix = entry + "\n"
    return (
        update(MemoryDocument)
        .where(MemoryDocument.id == doc_id)
        .values(
            history_text=sa_case(
                (MemoryDocument.history_text.is_(None), literal_column("''")),
                else_=MemoryDocument.history_text,
            )
            + suffix
        )
        .execution_options(synchronize_session="fetch")
    )


def _strip_section_prefix(raw: str, prefix: str) -> str:
    """Strip an optional leading ``# Soul`` / ``# User`` header.

    Pulled out of the read methods so sync and async produce the same
    string for the same DB content.
    """
    raw = raw.strip()
    if raw.startswith(prefix):
        raw = raw[len(prefix) :].strip()
    return raw


class MemoryStore:
    """Database-backed memory storage using MemoryDocument ORM model.

    Dual-API store (issue #1153, part of #1139). Each public sync
    method has an ``*_async`` peer with identical semantics. The two
    paths share query construction via the module-level builders
    above; only the session acquisition and ``await`` placement
    differ. Sync callers (CLI, premium, legacy paths) keep working
    unchanged while OSS-internal callers migrate to the async API one
    site at a time. Follows the IdempotencyStore pilot from PR #1199.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    # -- internal helpers --------------------------------------------------

    def _get_or_create_doc(self, db: Session) -> MemoryDocument:
        """Get or create the MemoryDocument row for this user."""
        doc = db.execute(_doc_select(self.user_id)).scalar_one_or_none()
        if doc is None:
            doc = MemoryDocument(user_id=self.user_id, memory_text="", history_text="")
            db.add(doc)
            db.flush()
        return doc

    async def _get_or_create_doc_async(self, db: AsyncSession) -> MemoryDocument:
        """Async peer of ``_get_or_create_doc``."""
        doc = (await db.execute(_doc_select(self.user_id))).scalar_one_or_none()
        if doc is None:
            doc = MemoryDocument(user_id=self.user_id, memory_text="", history_text="")
            db.add(doc)
            await db.flush()
        return doc

    # -- memory text -------------------------------------------------------

    def read_memory(self) -> str:
        """Read memory text (equivalent of MEMORY.md)."""
        db = SessionLocal()
        try:
            doc = db.execute(_doc_select(self.user_id)).scalar_one_or_none()
            if doc is None:
                return ""
            return (doc.memory_text or "").strip()
        finally:
            db.close()

    async def read_memory_async(self) -> str:
        """Async peer of ``read_memory``."""
        db = AsyncSessionLocal()
        try:
            doc = (await db.execute(_doc_select(self.user_id))).scalar_one_or_none()
            if doc is None:
                return ""
            return (doc.memory_text or "").strip()
        finally:
            await db.close()

    def write_memory(self, content: str) -> None:
        """Write memory text (full rewrite, equivalent of MEMORY.md)."""
        with db_session() as db:
            doc = self._get_or_create_doc(db)
            doc.memory_text = content.rstrip() + "\n"
            db.commit()

    async def write_memory_async(self, content: str) -> None:
        """Async peer of ``write_memory``."""
        async with db_session_async() as db:
            doc = await self._get_or_create_doc_async(db)
            doc.memory_text = content.rstrip() + "\n"
            await db.commit()

    # -- history text ------------------------------------------------------

    def read_history(self) -> str:
        """Read history text (equivalent of HISTORY.md)."""
        db = SessionLocal()
        try:
            doc = db.execute(_doc_select(self.user_id)).scalar_one_or_none()
            if doc is None:
                return ""
            return (doc.history_text or "").strip()
        finally:
            db.close()

    async def read_history_async(self) -> str:
        """Async peer of ``read_history``."""
        db = AsyncSessionLocal()
        try:
            doc = (await db.execute(_doc_select(self.user_id))).scalar_one_or_none()
            if doc is None:
                return ""
            return (doc.history_text or "").strip()
        finally:
            await db.close()

    async def append_history(self, entry: str) -> None:
        """Append an entry to history text (equivalent of HISTORY.md).

        Uses SQL-level concatenation to avoid lost-update races when
        two callers append concurrently.
        """
        with db_session() as db:
            doc = self._get_or_create_doc(db)
            db.execute(_append_history_update(doc.id, entry))
            db.commit()

    async def append_history_async(self, entry: str) -> None:
        """Async peer of ``append_history``.

        Same SQL-level concatenation contract as the sync path; the
        only difference is session acquisition and ``await``
        placement.
        """
        async with db_session_async() as db:
            doc = await self._get_or_create_doc_async(db)
            await db.execute(_append_history_update(doc.id, entry))
            await db.commit()

    # -- soul text ---------------------------------------------------------

    def read_soul(self) -> str:
        """Read soul text from User model."""
        db = SessionLocal()
        try:
            user = db.execute(_user_select(self.user_id)).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.soul_text or "", "# Soul")
        finally:
            db.close()

    async def read_soul_async(self) -> str:
        """Async peer of ``read_soul``."""
        db = AsyncSessionLocal()
        try:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.soul_text or "", "# Soul")
        finally:
            await db.close()

    def write_soul(self, content: str) -> None:
        """Write soul text to User model."""
        with db_session() as db:
            user = db.execute(_user_select(self.user_id)).scalar_one_or_none()
            if user is not None:
                user.soul_text = f"# Soul\n\n{content}\n"
                db.commit()

    async def write_soul_async(self, content: str) -> None:
        """Async peer of ``write_soul``."""
        async with db_session_async() as db:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is not None:
                user.soul_text = f"# Soul\n\n{content}\n"
                await db.commit()

    # -- user text ---------------------------------------------------------

    def read_user(self) -> str:
        """Read user text from User model."""
        db = SessionLocal()
        try:
            user = db.execute(_user_select(self.user_id)).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.user_text or "", "# User")
        finally:
            db.close()

    async def read_user_async(self) -> str:
        """Async peer of ``read_user``."""
        db = AsyncSessionLocal()
        try:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.user_text or "", "# User")
        finally:
            await db.close()

    def write_user(self, content: str) -> None:
        """Write user text to User model."""
        with db_session() as db:
            user = db.execute(_user_select(self.user_id)).scalar_one_or_none()
            if user is not None:
                user.user_text = f"# User\n\n{content}\n"
                db.commit()

    async def write_user_async(self, content: str) -> None:
        """Async peer of ``write_user``."""
        async with db_session_async() as db:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is not None:
                user.user_text = f"# User\n\n{content}\n"
                await db.commit()

    # -- composite helpers -------------------------------------------------

    async def build_memory_context(self) -> str:
        """Build memory context for injection into the agent prompt."""
        return self.read_memory()

    async def build_memory_context_async(self) -> str:
        """Async peer of ``build_memory_context``."""
        return await self.read_memory_async()


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
