"""Database-backed memory store.

Replaces FileMemoryStore from file_store.py. Uses MemoryDocument ORM model
for MEMORY.md and HISTORY.md content, and User ORM model for soul_text and
user_text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import Select, TextClause, Update, select, text, update

from backend.app.agent.markdown_registry import (
    append_with_window,
    assert_within_budget,
    get_policy,
)
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


def _user_select_for_update(user_id: str) -> Select[tuple[User]]:
    """Builder for the locked read used by compare-and-swap writes.

    ``write_user_async`` / ``write_soul_async`` re-read the row under
    ``FOR UPDATE`` when the caller supplies *expected_current*, so the
    compare and the write are atomic against concurrent writers (the
    agent's workspace tools, another compaction). See issue #1429.
    """
    return select(User).filter_by(id=user_id).with_for_update()


def _doc_select_for_update(user_id: str) -> Select[tuple[MemoryDocument]]:
    """Builder for the locked read used by ``append_history``.

    ``MemoryDocument.history_text`` is an ``EncryptedString`` column,
    so we cannot append ciphertext on the SQL side: each row carries
    its own DEK and the envelope format is not concat-friendly.
    Instead, the append path SELECTs the row under ``FOR UPDATE``,
    decrypts automatically on read, concatenates in Python, and writes
    the full new plaintext back. The row-level lock serializes
    concurrent appenders against an existing row; first-append
    callers (no row yet) are serialized by the per-user advisory
    lock in ``append_history`` because ``FOR UPDATE`` on a missing
    row acquires no predicate lock.
    """
    return select(MemoryDocument).filter_by(user_id=user_id).with_for_update()


def _advisory_lock_key(user_id: str) -> str:
    """Stable string key for the per-user MemoryDocument advisory lock."""
    return f"memory_doc:{user_id}"


def _advisory_lock_sql() -> TextClause:
    """``pg_advisory_xact_lock`` SQL bound to a ``:k`` parameter.

    The lock is transaction-scoped and released only on COMMIT / ROLLBACK,
    not when the Python execute() returns. Caller binds ``{"k": <key>}``.
    Mirrors the pattern in ``backend/app/agent/session_db.py``.
    """
    return text("SELECT pg_advisory_xact_lock(hashtext(:k))")


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
    ``*_async`` peers remain, plus ``append_history`` which keeps its
    historical plain name (issue #1221). Premium and OSS callers all reach
    for the async API directly.
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

    async def write_memory_async(
        self, content: str, *, expected_current: str | None = None
    ) -> bool:
        """Write memory text (full rewrite, equivalent of MEMORY.md).

        Returns True when the write landed. When *expected_current* is
        provided, the write is compare-and-swap: the row is re-read under
        the per-user advisory lock plus ``FOR UPDATE``, and when the
        stored text no longer matches (another writer landed since the
        caller's read: the agent's workspace tools mid-conversation, or a
        concurrent compaction), nothing is written and False is returned.
        Full rewrites computed from a stale read must not clobber a newer
        value (issue #1429). *expected_current* is compared in the same
        normalized form :meth:`read_memory_async` returns (stripped).

        Raises :class:`BudgetExceededError` when the value exceeds the
        ``MEMORY.md`` byte budget declared in
        :mod:`backend.app.agent.markdown_registry`. Callers that may
        produce LLM-generated rewrites (compaction) are expected to
        catch and log this rather than crash.
        """
        stored = content.rstrip() + "\n"
        assert_within_budget("MEMORY.md", stored)
        async with db_session_async() as db:
            if expected_current is None:
                doc = await self._get_or_create_doc_async(db)
            else:
                # Advisory lock so the no-row-yet branch cannot race a
                # concurrent first writer (FOR UPDATE on a missing row
                # acquires no predicate lock; same rationale as
                # ``append_history``).
                await db.execute(
                    _advisory_lock_sql(),
                    {"k": _advisory_lock_key(self.user_id)},
                )
                doc = (await db.execute(_doc_select_for_update(self.user_id))).scalar_one_or_none()
                if doc is None:
                    doc = MemoryDocument(user_id=self.user_id, memory_text="", history_text="")
                    db.add(doc)
                    await db.flush()
                if (doc.memory_text or "").strip() != expected_current.strip():
                    # Early return without commit: db_session_async closes
                    # the session, rolling back the open transaction and
                    # releasing the row lock.
                    return False
            doc.memory_text = stored
            await db.commit()
            return True

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

    async def append_history(self, entry: str) -> str:
        """Append an entry to history text (equivalent of HISTORY.md).

        Reads the current row under ``SELECT ... FOR UPDATE`` to
        serialize concurrent appenders, decrypts and concatenates in
        Python, then rewrites the column with the full plaintext.
        SQL-side concatenation is not viable because ``history_text``
        is an ``EncryptedString`` column whose envelope format is not
        concat-safe.

        Takes a per-user ``pg_advisory_xact_lock`` at the top so the
        first-append branch (no row yet) cannot race: ``FOR UPDATE``
        on a missing row acquires no predicate lock, so two concurrent
        first-appends would otherwise both see ``None``, both INSERT,
        and one would lose to ``uq_memory_documents_user_id``
        (issue #1224). The lock is released automatically on COMMIT
        or ROLLBACK of the surrounding transaction.

        Guarantees a newline between the existing text and the new
        entry: if the stored text is non-empty and does not already
        end with a newline (e.g. a manual edit, or older text written
        before this guarantee), a separator is inserted so two entries
        never end up jammed together as one line.

        Applies the HISTORY.md byte budget windowing policy from
        :mod:`backend.app.agent.markdown_registry`: when the post-append
        text would exceed the budget, the oldest entries are dropped
        whole (FIFO). This bounds runaway growth on long-lived users
        without losing the row-level lock semantics. The full archive
        of compaction events still lives in ``compaction_events`` rows.

        Returns the row's new full plaintext so callers (compaction
        audit) can record the post-append snapshot without re-reading
        the row, which would race with concurrent compactions sharing
        the same user.
        """
        policy = get_policy("HISTORY.md")
        budget = policy.byte_budget if policy is not None else None
        async with db_session_async() as db:
            await db.execute(
                _advisory_lock_sql(),
                {"k": _advisory_lock_key(self.user_id)},
            )
            doc = (await db.execute(_doc_select_for_update(self.user_id))).scalar_one_or_none()
            if doc is None:
                full_new_text = (
                    append_with_window("", entry, budget) if budget is not None else entry + "\n"
                )
                db.add(
                    MemoryDocument(
                        user_id=self.user_id,
                        memory_text="",
                        history_text=full_new_text,
                    )
                )
                await db.commit()
                return full_new_text
            current = doc.history_text or ""
            if budget is not None:
                full_new_text = append_with_window(current, entry, budget)
            else:
                if current and not current.endswith("\n"):
                    current += "\n"
                full_new_text = current + entry + "\n"
            await db.execute(_append_history_update(doc.id, full_new_text))
            await db.commit()
            return full_new_text

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

    async def write_soul_async(self, content: str, *, expected_current: str | None = None) -> bool:
        """Write soul text to User model.

        Returns True when the write landed. When *expected_current* is
        provided, the write is compare-and-swap against the current value
        in the same normalized form :meth:`read_soul_async` returns; see
        :meth:`write_memory_async` for the rationale (issue #1429).

        Raises :class:`BudgetExceededError` when the wrapped value
        exceeds the ``SOUL.md`` byte budget. Compaction wraps the call
        in a try/except and logs on failure rather than crashing.
        """
        stored = f"# Soul\n\n{content}\n"
        assert_within_budget("SOUL.md", stored)
        async with db_session_async() as db:
            stmt = (
                _user_select(self.user_id)
                if expected_current is None
                else _user_select_for_update(self.user_id)
            )
            user = (await db.execute(stmt)).scalar_one_or_none()
            if user is None:
                return False
            if expected_current is not None:
                current = _strip_section_prefix(user.soul_text or "", "# Soul")
                if current != expected_current.strip():
                    return False
            user.soul_text = stored
            await db.commit()
            return True

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

    async def write_user_async(self, content: str, *, expected_current: str | None = None) -> bool:
        """Write user text to User model.

        Returns True when the write landed. When *expected_current* is
        provided, the write is compare-and-swap against the current value
        in the same normalized form :meth:`read_user_async` returns; see
        :meth:`write_memory_async` for the rationale (issue #1429).

        Raises :class:`BudgetExceededError` when the wrapped value
        exceeds the ``USER.md`` byte budget. Compaction wraps the call
        in a try/except and logs on failure rather than crashing.
        """
        stored = f"# User\n\n{content}\n"
        assert_within_budget("USER.md", stored)
        async with db_session_async() as db:
            stmt = (
                _user_select(self.user_id)
                if expected_current is None
                else _user_select_for_update(self.user_id)
            )
            user = (await db.execute(stmt)).scalar_one_or_none()
            if user is None:
                return False
            if expected_current is not None:
                current = _strip_section_prefix(user.user_text or "", "# User")
                if current != expected_current.strip():
                    return False
            user.user_text = stored
            await db.commit()
            return True

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
