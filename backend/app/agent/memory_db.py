"""Database-backed memory store.

Replaces FileMemoryStore from file_store.py. Uses MemoryDocument ORM model
for MEMORY.md and HISTORY.md content, and User ORM model for soul_text and
user_text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import Select, Update, select, update

from backend.app.agent.store_cache import StoreCache
from backend.app.database import (
    AsyncSessionLocal,
    db_session_async,
)
from backend.app.models import MemoryDocument, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure typed builders shared by every async store method.
#
# Originally introduced as the dual sync/async pilot from issue #1150 /
# PR #1199; the sync surface has since been removed (issue #1160 final
# pass). Builders stay because they keep query construction in one place
# and return concretely-typed SQLAlchemy core constructs that ``ty`` can
# verify end-to-end without ``Any``.
# ---------------------------------------------------------------------------


def _doc_select(user_id: str) -> Select[tuple[MemoryDocument]]:
    """Builder shared by every read of the user's MemoryDocument row."""
    return select(MemoryDocument).filter_by(user_id=user_id)


def _user_select(user_id: str) -> Select[tuple[User]]:
    """Builder shared by every read of the user's User row."""
    return select(User).filter_by(id=user_id)


def _doc_select_for_update(user_id: str) -> Select[tuple[MemoryDocument]]:
    """Builder for the locked read used by ``append_history``.

    ``MemoryDocument.history_text`` is an ``EncryptedString`` column,
    so we cannot append ciphertext on the SQL side: each row carries
    its own DEK and the envelope format is not concat-friendly.
    Instead, the append path SELECTs the row under ``FOR UPDATE``,
    decrypts automatically on read, concatenates in Python, and writes
    the full new plaintext back. The row-level lock serializes
    concurrent appenders so neither side loses its update.
    """
    return select(MemoryDocument).filter_by(user_id=user_id).with_for_update()


def _append_history_update(doc_id: int, full_new_text: str) -> Update:
    """Build the UPDATE used by the ``append_history`` path.

    The caller has already read the current ``history_text`` under a
    row-level lock, decrypted it, appended the new entry in Python, and
    passes the full plaintext here. Encryption is automatic on bind,
    so the column is rewritten with a fresh envelope every time.
    """
    return (
        update(MemoryDocument)
        .where(MemoryDocument.id == doc_id)
        .values(history_text=full_new_text)
        .execution_options(synchronize_session="fetch")
    )


def _strip_section_prefix(raw: str, prefix: str) -> str:
    """Strip an optional leading ``# Soul`` / ``# User`` header.

    Pulled out of the read methods so the public API and any tests
    that read the column directly produce the same string.
    """
    raw = raw.strip()
    if raw.startswith(prefix):
        raw = raw[len(prefix) :].strip()
    return raw


class MemoryStore:
    """Database-backed memory storage using MemoryDocument ORM model.

    Async-only API after the issue #1160 final pass. The dual sync/async
    surface from issue #1153 (PR #1199 pilot) has been collapsed: only the
    ``*_async`` peers remain. Premium and OSS callers all reach for the
    async API directly.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    # -- internal helpers --------------------------------------------------

    async def _get_or_create_doc_async(self, db: AsyncSession) -> MemoryDocument:
        """Get or create the MemoryDocument row for this user."""
        doc = (await db.execute(_doc_select(self.user_id))).scalar_one_or_none()
        if doc is None:
            doc = MemoryDocument(user_id=self.user_id, memory_text="", history_text="")
            db.add(doc)
            await db.flush()
        return doc

    # -- memory text -------------------------------------------------------

    async def read_memory_async(self) -> str:
        """Read memory text (equivalent of MEMORY.md)."""
        db = AsyncSessionLocal()
        try:
            doc = (await db.execute(_doc_select(self.user_id))).scalar_one_or_none()
            if doc is None:
                return ""
            return (doc.memory_text or "").strip()
        finally:
            await db.close()

    async def write_memory_async(self, content: str) -> None:
        """Write memory text (full rewrite, equivalent of MEMORY.md)."""
        async with db_session_async() as db:
            doc = await self._get_or_create_doc_async(db)
            doc.memory_text = content.rstrip() + "\n"
            await db.commit()

    # -- history text ------------------------------------------------------

    async def read_history_async(self) -> str:
        """Read history text (equivalent of HISTORY.md)."""
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

        Reads the current row under ``SELECT ... FOR UPDATE`` to
        serialize concurrent appenders, decrypts and concatenates in
        Python, then rewrites the column with the full plaintext.
        SQL-side concatenation is not viable because ``history_text``
        is an ``EncryptedString`` column whose envelope format is not
        concat-safe.
        """
        suffix = entry + "\n"
        async with db_session_async() as db:
            doc = (await db.execute(_doc_select_for_update(self.user_id))).scalar_one_or_none()
            if doc is None:
                db.add(
                    MemoryDocument(
                        user_id=self.user_id,
                        memory_text="",
                        history_text=suffix,
                    )
                )
            else:
                full_new_text = (doc.history_text or "") + suffix
                await db.execute(_append_history_update(doc.id, full_new_text))
            await db.commit()

    async def append_history_async(self, entry: str) -> None:
        """Deprecated alias of :meth:`append_history`."""
        await self.append_history(entry)

    # -- soul text ---------------------------------------------------------

    async def read_soul_async(self) -> str:
        """Read soul text from User model."""
        db = AsyncSessionLocal()
        try:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.soul_text or "", "# Soul")
        finally:
            await db.close()

    async def write_soul_async(self, content: str) -> None:
        """Write soul text to User model."""
        async with db_session_async() as db:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is not None:
                user.soul_text = f"# Soul\n\n{content}\n"
                await db.commit()

    # -- user text ---------------------------------------------------------

    async def read_user_async(self) -> str:
        """Read user text from User model."""
        db = AsyncSessionLocal()
        try:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is None:
                return ""
            return _strip_section_prefix(user.user_text or "", "# User")
        finally:
            await db.close()

    async def write_user_async(self, content: str) -> None:
        """Write user text to User model."""
        async with db_session_async() as db:
            user = (await db.execute(_user_select(self.user_id))).scalar_one_or_none()
            if user is not None:
                user.user_text = f"# User\n\n{content}\n"
                await db.commit()

    # -- composite helpers -------------------------------------------------

    async def build_memory_context_async(self) -> str:
        """Build memory context for injection into the agent prompt."""
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
    return await store.build_memory_context_async()


async def read_memory(user_id: str) -> str:
    """Read raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    return await store.read_memory_async()


async def write_memory(user_id: str, content: str) -> None:
    """Write raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    await store.write_memory_async(content)
