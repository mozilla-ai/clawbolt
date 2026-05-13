"""Database-backed session store.

Replaces FileSessionStore from file_store.py. Uses ChatSession and Message
ORM models for persistence, while keeping SessionState and StoredMessage
Pydantic models as in-memory DTOs.

Dual-API rollout (issue #1152, part of #1139): each public sync method
has an ``*_async`` peer with identical semantics; the two share query
construction via the ``_*_select`` / ``_*_delete`` / ``_advisory_*``
builders below. Mirrors the pilot in
``backend/app/agent/stores.py::IdempotencyStore`` (issue #1150 / PR #1199).
Sync callers (CLI, Alembic, premium during the migration window) keep
working unchanged while OSS-internal callers migrate to the async API
one site at a time. SessionStore is the largest single store in the
rollout, so this file is also the largest single conversion.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, cast

from sqlalchemy import (
    CursorResult,
    Delete,
    Select,
    TextClause,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.agent.store_cache import StoreCache
from backend.app.config import settings
from backend.app.database import (
    AsyncSessionLocal,
    db_session_async,
)
from backend.app.models import ChatSession, Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ORM -> DTO converters
# ---------------------------------------------------------------------------


def _msg_to_stored(msg: Message) -> StoredMessage:
    """Convert a Message ORM object to a StoredMessage DTO."""
    ts = msg.timestamp.isoformat() if msg.timestamp else ""
    return StoredMessage(
        direction=msg.direction,
        body=msg.body,
        processed_context=msg.processed_context,
        tool_interactions_json=msg.tool_interactions_json,
        thinking_text=msg.thinking_text,
        external_message_id=msg.external_message_id,
        media_urls_json=msg.media_urls_json,
        timestamp=ts,
        seq=msg.seq,
    )


def _session_to_state(
    cs: ChatSession,
    messages: list[Message] | None = None,
) -> SessionState:
    """Convert a ChatSession ORM object to a SessionState DTO."""
    msgs = messages if messages is not None else []
    return SessionState(
        session_id=cs.session_id,
        user_id=cs.user_id,
        messages=[_msg_to_stored(m) for m in sorted(msgs, key=lambda m: m.seq)],
        created_at=cs.created_at.isoformat() if cs.created_at else "",
        last_message_at=cs.last_message_at.isoformat() if cs.last_message_at else "",
        channel=cs.channel,
        initial_system_prompt=cs.initial_system_prompt,
        last_trim_seq=cs.last_trim_seq,
    )


# ---------------------------------------------------------------------------
# Pure typed builders shared by sync and async paths
# ---------------------------------------------------------------------------
#
# Each builder returns a fully typed ``Select`` / ``Delete`` / ``TextClause``
# so the sync and async methods stay in lockstep without subclassing.
# Mirrors ``backend/app/agent/stores.py``'s pilot pattern (PR #1199).
# Builders never touch a session and never return ``Any``; ``ty`` enforces
# this at the Definition of Done.


def _advisory_lock_key(user_id: str) -> str:
    """Stable string key for ``pg_advisory_xact_lock`` keyed on user_id."""
    return f"session_create:{user_id}"


def _advisory_lock_sql() -> TextClause:
    """``pg_advisory_xact_lock`` SQL bound to a ``:k`` parameter.

    The lock is transaction-scoped and released only on COMMIT / ROLLBACK,
    not when the Python execute() returns. Caller binds ``{"k": <key>}``.
    """
    return text("SELECT pg_advisory_xact_lock(hashtext(:k))")


def _select_session_by_session_id(session_id: str, user_id: str) -> Select[tuple[ChatSession]]:
    return select(ChatSession).filter_by(session_id=session_id, user_id=user_id)


def _select_session_by_user(user_id: str) -> Select[tuple[ChatSession]]:
    return select(ChatSession).filter_by(user_id=user_id)


def _select_all_sessions_for_user(user_id: str) -> Select[tuple[ChatSession]]:
    return select(ChatSession).filter_by(user_id=user_id).order_by(ChatSession.created_at)


def _select_session_for_update(cs_id: int) -> Select[tuple[ChatSession]]:
    """Lock the session row to serialize concurrent message inserts.

    PostgreSQL's ``FOR UPDATE`` cannot be used with aggregates, so we lock
    the parent row and then read ``max(Message.seq)`` separately.
    """
    return select(ChatSession).filter_by(id=cs_id).with_for_update()


def _select_messages_for_session(cs_id: int) -> Select[tuple[Message]]:
    return select(Message).filter_by(session_id=cs_id).order_by(Message.seq)


def _select_message_by_seq(cs_id: int, seq: int) -> Select[tuple[Message]]:
    return select(Message).filter_by(session_id=cs_id, seq=seq)


def _select_max_seq(cs_id: int) -> Select[tuple[int | None]]:
    return select(func.max(Message.seq)).filter_by(session_id=cs_id)


def _select_last_timestamp(user_id: str, direction: str) -> Select[tuple[datetime.datetime | None]]:
    return (
        select(func.max(Message.timestamp))
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id, Message.direction == direction)
    )


def _select_recent_messages(
    user_id: str,
    count: int,
    exclude_session_id: str | None = None,
) -> Select[tuple[Message]]:
    """Most recent ``count`` messages across the user's sessions.

    Returned in DESC order so the caller can ``reversed(...)`` to render
    chronologically; the ORDER BY is on the SQL side so LIMIT applies to
    the newest tail rather than to an arbitrary slice.
    """
    stmt = (
        select(Message)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id)
    )
    if exclude_session_id:
        stmt = stmt.where(ChatSession.session_id != exclude_session_id)
    return stmt.order_by(Message.timestamp.desc()).limit(count)


def _delete_message_by_seq(cs_id: int, seq: int) -> Delete[tuple[Message]]:
    return (
        delete(Message)
        .where(Message.session_id == cs_id, Message.seq == seq)
        .execution_options(synchronize_session="fetch")
    )


def _delete_messages_by_seqs(cs_id: int, seqs: list[int]) -> Delete[tuple[Message]]:
    return (
        delete(Message)
        .where(Message.session_id == cs_id, Message.seq.in_(seqs))
        .execution_options(synchronize_session="fetch")
    )


def _delete_all_messages_for_session(cs_id: int) -> Delete[tuple[Message]]:
    return (
        delete(Message)
        .where(Message.session_id == cs_id)
        .execution_options(synchronize_session="fetch")
    )


async def _reset_trim_watermark_if_orphaned(db: AsyncSession, cs: ChatSession) -> None:
    """Reset ``last_trim_seq`` to None when nothing above the watermark remains.

    ``load_conversation_history`` filters to ``seq > last_trim_seq``, and new
    inserts pick the next seq via ``max(seq) + 1`` (which is 1 when the table
    is empty). If a partial delete (via :meth:`SessionStore.delete_message_async`
    or :meth:`SessionStore.delete_messages_by_seqs_async`) removes every row
    above the watermark, the watermark becomes a permanent ceiling: every
    future inserted message satisfies ``seq <= last_trim_seq`` and is silently
    filtered out of LLM context. Same failure mode the docstring on
    ``delete_messages_async`` warns about, just via the per-seq paths.

    Resetting here drops the watermark only when there is nothing left for it
    to protect; trimmed facts already live in MEMORY.md / USER.md / SOUL.md.
    """
    if cs.last_trim_seq is None:
        return
    max_seq = (await db.execute(_select_max_seq(cs.id))).scalar()
    if max_seq is None or max_seq <= cs.last_trim_seq:
        cs.last_trim_seq = None


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


_MESSAGE_UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "body",
        "processed_context",
        "tool_interactions_json",
        "thinking_text",
        "external_message_id",
        "media_urls_json",
    }
)


class SessionStore:
    """Database-backed session storage using ChatSession and Message ORM models.

    Async-only API after the issue #1160 final pass. The dual-API surface
    from issue #1152 has been collapsed: only the async implementations
    remain. The bare-name methods now delegate to their ``*_async`` peers
    to keep the public surface stable for any out-of-tree caller; OSS and
    premium have all migrated to the suffixed names.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    # ------------------------------------------------------------------
    # load_session
    # ------------------------------------------------------------------

    async def load_session_async(self, session_id: str) -> SessionState | None:
        """Load a session by its string session_id."""
        db = AsyncSessionLocal()
        try:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return None
            messages = list((await db.execute(_select_messages_for_session(cs.id))).scalars().all())
            return _session_to_state(cs, messages)
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # list_sessions
    # ------------------------------------------------------------------

    async def list_sessions_async(self) -> list[SessionState]:
        """Return all sessions with their messages for this user."""
        db = AsyncSessionLocal()
        try:
            sessions = (
                (await db.execute(_select_all_sessions_for_user(self.user_id))).scalars().all()
            )
            result = []
            for cs in sessions:
                messages = list(
                    (await db.execute(_select_messages_for_session(cs.id))).scalars().all()
                )
                result.append(_session_to_state(cs, messages))
            return result
        finally:
            await db.close()

    async def list_sessions(self) -> list[SessionState]:
        """Deprecated alias of :meth:`list_sessions_async`."""
        return await self.list_sessions_async()

    # ------------------------------------------------------------------
    # get_or_create_session  (advisory-lock site)
    # ------------------------------------------------------------------

    async def get_or_create_session(self) -> tuple[SessionState, bool]:
        """Deprecated alias of :meth:`get_or_create_session_async`."""
        return await self.get_or_create_session_async()

    async def get_or_create_session_async(self) -> tuple[SessionState, bool]:
        """Get the user's session, creating it on first call.

        Each user has a single persistent session, enforced by the
        ``uq_sessions_user_id`` UNIQUE constraint. Returns
        ``(session, is_new)`` where ``is_new`` is True only on the very
        first call for a user.

        Concurrent first-message arrivals on different channels would
        otherwise race the INSERT and one would lose to the unique
        constraint. We serialize with a transaction-scoped advisory lock
        keyed on user_id so the runner-up sees the winner's row instead.

        Preserves the ``pg_advisory_xact_lock`` semantics: the lock is
        acquired inside an autobegun transaction and released only on
        COMMIT / ROLLBACK of that transaction. Each ``await db.commit()``
        below ends the transaction the lock was attached to, so a follow-up
        execute() will autobegin a fresh transaction without the lock,
        which is fine because the first commit already published the
        session row to other waiters.
        """
        async with db_session_async() as db:
            await db.execute(
                _advisory_lock_sql(),
                {"k": _advisory_lock_key(self.user_id)},
            )
            cs = (await db.execute(_select_session_by_user(self.user_id))).scalar_one_or_none()
            if cs is not None:
                now = datetime.datetime.now(datetime.UTC)
                cs.last_message_at = now
                await db.commit()
                messages = list(
                    (await db.execute(_select_messages_for_session(cs.id))).scalars().all()
                )
                return _session_to_state(cs, messages), False

            now = datetime.datetime.now(datetime.UTC)
            ts = int(now.timestamp())
            short_uid = uuid.uuid4().hex[:8]
            session_id = f"{self.user_id}_{ts}_{short_uid}"

            cs = ChatSession(
                session_id=session_id,
                user_id=self.user_id,
                channel="",
                created_at=now,
                last_message_at=now,
            )
            db.add(cs)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                cs = (await db.execute(_select_session_by_user(self.user_id))).scalar_one_or_none()
                if cs is not None:
                    messages = list(
                        (await db.execute(_select_messages_for_session(cs.id))).scalars().all()
                    )
                    return _session_to_state(cs, messages), False
                short_uid = uuid.uuid4().hex[:8]
                session_id = f"{self.user_id}_{ts}_{short_uid}"
                cs = ChatSession(
                    session_id=session_id,
                    user_id=self.user_id,
                    channel="",
                    created_at=now,
                    last_message_at=now,
                )
                db.add(cs)
                await db.commit()
            await db.refresh(cs)
            return _session_to_state(cs, []), True

    # ------------------------------------------------------------------
    # add_message
    # ------------------------------------------------------------------

    async def add_message(
        self,
        session: SessionState,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        thinking_text: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Deprecated alias of :meth:`add_message_async`."""
        return await self.add_message_async(
            session,
            direction,
            body,
            external_message_id=external_message_id,
            media_urls_json=media_urls_json,
            processed_context=processed_context,
            tool_interactions_json=tool_interactions_json,
            thinking_text=thinking_text,
            channel=channel,
        )

    async def add_message_async(
        self,
        session: SessionState,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        thinking_text: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Insert a message into the database and update the in-memory session."""
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session.session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                now = datetime.datetime.now(datetime.UTC)
                cs = ChatSession(
                    session_id=session.session_id,
                    user_id=session.user_id,
                    channel=channel or session.channel,
                    created_at=now,
                    last_message_at=now,
                )
                db.add(cs)
                await db.flush()

            await db.execute(_select_session_for_update(cs.id))
            max_seq: int = (await db.execute(_select_max_seq(cs.id))).scalar() or 0
            seq = max_seq + 1
            now = datetime.datetime.now(datetime.UTC)

            msg = Message(
                session_id=cs.id,
                seq=seq,
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                thinking_text=thinking_text,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now,
            )
            db.add(msg)

            cs.last_message_at = now
            if channel:
                cs.channel = channel

            await db.commit()

            stored = StoredMessage(
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                thinking_text=thinking_text,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now.isoformat(),
                seq=seq,
            )
            session.messages.append(stored)
            session.last_message_at = now.isoformat()
            if channel:
                session.channel = channel

            return stored

    # ------------------------------------------------------------------
    # add_message_by_session_id
    # ------------------------------------------------------------------

    async def add_message_by_session_id(
        self,
        session_id: str,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        thinking_text: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Deprecated alias of :meth:`add_message_by_session_id_async`."""
        return await self.add_message_by_session_id_async(
            session_id,
            direction,
            body,
            external_message_id=external_message_id,
            media_urls_json=media_urls_json,
            processed_context=processed_context,
            tool_interactions_json=tool_interactions_json,
            thinking_text=thinking_text,
            channel=channel,
        )

    async def add_message_by_session_id_async(
        self,
        session_id: str,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        thinking_text: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Insert a message using only the session_id (no SessionState needed).

        Useful when the caller does not have a live ``SessionState`` object,
        e.g. persisting an approval prompt from the agent loop.
        """
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                raise ValueError(f"Session {session_id!r} not found for user {self.user_id!r}")

            await db.execute(_select_session_for_update(cs.id))
            max_seq: int = (await db.execute(_select_max_seq(cs.id))).scalar() or 0
            seq = max_seq + 1
            now = datetime.datetime.now(datetime.UTC)

            msg = Message(
                session_id=cs.id,
                seq=seq,
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                thinking_text=thinking_text,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now,
            )
            db.add(msg)
            cs.last_message_at = now
            if channel:
                cs.channel = channel
            await db.commit()

            return StoredMessage(
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                thinking_text=thinking_text,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now.isoformat(),
                seq=seq,
            )

    # ------------------------------------------------------------------
    # update_message
    # ------------------------------------------------------------------

    async def update_message(
        self,
        session: SessionState,
        seq: int,
        **updates: Any,
    ) -> None:
        """Deprecated alias of :meth:`update_message_async`."""
        await self.update_message_async(session, seq, **updates)

    async def update_message_async(
        self,
        session: SessionState,
        seq: int,
        **updates: Any,
    ) -> None:
        """Update a message by seq number."""
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session.session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return

            msg = (await db.execute(_select_message_by_seq(cs.id, seq))).scalar_one_or_none()
            if msg is None:
                return

            for key, value in updates.items():
                if key in _MESSAGE_UPDATABLE_FIELDS:
                    setattr(msg, key, value)
            await db.commit()

            for m in session.messages:
                if m.seq == seq:
                    for k, v in updates.items():
                        if k in _MESSAGE_UPDATABLE_FIELDS and hasattr(m, k):
                            setattr(m, k, v)
                    break

    # ------------------------------------------------------------------
    # update_initial_system_prompt
    # ------------------------------------------------------------------

    async def update_initial_system_prompt(self, session: SessionState, system_prompt: str) -> None:
        """Deprecated alias of :meth:`update_initial_system_prompt_async`."""
        await self.update_initial_system_prompt_async(session, system_prompt)

    async def update_initial_system_prompt_async(
        self, session: SessionState, system_prompt: str
    ) -> None:
        """Store the system prompt on the session if not already set."""
        if session.initial_system_prompt:
            return
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session.session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is not None and not cs.initial_system_prompt:
                cs.initial_system_prompt = system_prompt
                await db.commit()
            session.initial_system_prompt = system_prompt

    # ------------------------------------------------------------------
    # delete_message
    # ------------------------------------------------------------------

    async def delete_message_async(self, session_id: str, seq: int) -> bool:
        """Delete a single message by seq number from a session.

        Returns True if a message was deleted, False if not found.
        """
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return False
            result = await db.execute(_delete_message_by_seq(cs.id, seq))
            count: int = cast("CursorResult[object]", result).rowcount
            if count > 0:
                await _reset_trim_watermark_if_orphaned(db, cs)
            await db.commit()
            return count > 0

    # ------------------------------------------------------------------
    # delete_messages_by_seqs
    # ------------------------------------------------------------------

    async def delete_messages_by_seqs_async(self, session_id: str, seqs: list[int]) -> int:
        """Delete specific messages by seq numbers from a session.

        Returns the number of messages actually deleted.
        """
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return 0
            result = await db.execute(_delete_messages_by_seqs(cs.id, seqs))
            count: int = cast("CursorResult[object]", result).rowcount
            if count > 0:
                await _reset_trim_watermark_if_orphaned(db, cs)
            await db.commit()
            return count

    # ------------------------------------------------------------------
    # delete_messages
    # ------------------------------------------------------------------

    async def delete_messages_async(self, session_id: str) -> int:
        """Delete all messages from a session and clear its initial system prompt.

        Returns the number of messages deleted. The session row itself is
        preserved so the conversation can continue with an empty history.

        Also resets ``last_trim_seq`` to ``None``. After the delete, the
        next message inserted gets ``seq = max(seq)+1 = 1`` (because
        ``_select_max_seq`` returns 0 on an empty table), so any stale
        watermark left over from a prior trim would silently filter out
        every new message in ``load_conversation_history`` (which keeps
        only ``seq > last_trim_seq``). Symptom: the agent receives only
        the live inbound on every turn and behaves as if the conversation
        has just started, forever.
        """
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return 0
            result = await db.execute(_delete_all_messages_for_session(cs.id))
            count: int = cast("CursorResult[object]", result).rowcount
            cs.initial_system_prompt = ""
            cs.last_trim_seq = None
            await db.commit()
            return count

    # ------------------------------------------------------------------
    # last-timestamp helpers
    # ------------------------------------------------------------------

    async def _get_last_timestamp_async(self, direction: str) -> datetime.datetime | None:
        """Get the most recent message timestamp in the given direction."""
        db = AsyncSessionLocal()
        try:
            return (await db.execute(_select_last_timestamp(self.user_id, direction))).scalar()
        finally:
            await db.close()

    async def get_last_inbound_timestamp_async(self) -> datetime.datetime | None:
        """Get the most recent inbound message timestamp."""
        return await self._get_last_timestamp_async("inbound")

    async def get_last_outbound_timestamp_async(self) -> datetime.datetime | None:
        """Get the most recent outbound message timestamp."""
        return await self._get_last_timestamp_async("outbound")

    # ------------------------------------------------------------------
    # recent-message collectors
    # ------------------------------------------------------------------

    async def _collect_messages_async(
        self,
        count: int | None = None,
        exclude_session_id: str | None = None,
    ) -> list[StoredMessage]:
        """Collect the most recent messages, optionally excluding a session."""
        resolved = count if count is not None else settings.heartbeat_recent_messages_count
        db = AsyncSessionLocal()
        try:
            messages = list(
                (
                    await db.execute(
                        _select_recent_messages(self.user_id, resolved, exclude_session_id)
                    )
                )
                .scalars()
                .all()
            )
            # Return in chronological order
            return [_msg_to_stored(m) for m in reversed(messages)]
        finally:
            await db.close()

    async def get_recent_messages_async(self, count: int | None = None) -> list[StoredMessage]:
        """Get the most recent messages across all sessions."""
        return await self._collect_messages_async(count)

    async def get_other_session_messages_async(
        self,
        exclude_session_id: str,
        count: int | None = None,
    ) -> list[StoredMessage]:
        """Get recent messages from sessions other than *exclude_session_id*."""
        return await self._collect_messages_async(count, exclude_session_id)


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

_cache: StoreCache[SessionStore] = StoreCache(SessionStore)


def get_session_store(user_id: str) -> SessionStore:
    """Get or create a SessionStore for the given user.

    Uses an LRU cache bounded to 256 entries to prevent unbounded memory
    growth in multi-tenant deployments.
    """
    return _cache.get(user_id)


def reset_session_stores() -> None:
    """Clear the session store cache (for tests)."""
    _cache.clear()
