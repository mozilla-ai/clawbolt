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

from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.agent.store_cache import StoreCache
from backend.app.config import settings
from backend.app.database import (
    AsyncSessionLocal,
    SessionLocal,
    db_session,
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


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


_MESSAGE_UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "body",
        "processed_context",
        "tool_interactions_json",
        "external_message_id",
        "media_urls_json",
    }
)


class SessionStore:
    """Database-backed session storage using ChatSession and Message ORM models.

    Dual-API store (issue #1152). Each public sync method has an
    ``*_async`` peer with identical semantics; query construction is
    factored into the module-level builder helpers above so the two
    paths stay in lockstep. Sync callers keep working unchanged while
    OSS-internal callers migrate to the async API one site at a time.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    # ------------------------------------------------------------------
    # load_session
    # ------------------------------------------------------------------

    def load_session(self, session_id: str) -> SessionState | None:
        """Load a session by its string session_id."""
        db = SessionLocal()
        try:
            cs = db.execute(
                _select_session_by_session_id(session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                return None
            messages = list(db.execute(_select_messages_for_session(cs.id)).scalars().all())
            return _session_to_state(cs, messages)
        finally:
            db.close()

    async def load_session_async(self, session_id: str) -> SessionState | None:
        """Async peer of ``load_session``."""
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

    async def list_sessions(self) -> list[SessionState]:
        """Return all sessions with their messages for this user."""
        db = SessionLocal()
        try:
            sessions = db.execute(_select_all_sessions_for_user(self.user_id)).scalars().all()
            result = []
            for cs in sessions:
                messages = list(db.execute(_select_messages_for_session(cs.id)).scalars().all())
                result.append(_session_to_state(cs, messages))
            return result
        finally:
            db.close()

    async def list_sessions_async(self) -> list[SessionState]:
        """Async peer of ``list_sessions``."""
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

    # ------------------------------------------------------------------
    # get_or_create_session  (advisory-lock site)
    # ------------------------------------------------------------------

    async def get_or_create_session(self) -> tuple[SessionState, bool]:
        """Get the user's session, creating it on first call.

        Each user has a single persistent session, enforced by the
        ``uq_sessions_user_id`` UNIQUE constraint. Returns
        ``(session, is_new)`` where ``is_new`` is True only on the very
        first call for a user.

        Concurrent first-message arrivals on different channels would
        otherwise race the INSERT and one would lose to the unique
        constraint. We serialize with a transaction-scoped advisory lock
        keyed on user_id so the runner-up sees the winner's row instead.
        """
        db = SessionLocal()
        try:
            db.execute(
                _advisory_lock_sql(),
                {"k": _advisory_lock_key(self.user_id)},
            )
            cs = db.execute(_select_session_by_user(self.user_id)).scalar_one_or_none()
            if cs is not None:
                now = datetime.datetime.now(datetime.UTC)
                cs.last_message_at = now
                db.commit()
                messages = list(db.execute(_select_messages_for_session(cs.id)).scalars().all())
                return _session_to_state(cs, messages), False

            # Create new session with unique ID. Use timestamp + short UUID suffix
            # to keep IDs readable while avoiding races.
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
                db.commit()
            except IntegrityError:
                db.rollback()
                # Either a session_id collision (extremely unlikely) or
                # the user_id UNIQUE lost a race despite the advisory
                # lock. Reload and return the winner's row.
                cs = db.execute(_select_session_by_user(self.user_id)).scalar_one_or_none()
                if cs is not None:
                    messages = list(db.execute(_select_messages_for_session(cs.id)).scalars().all())
                    return _session_to_state(cs, messages), False
                # No conflicting row found; retry with a fresh session_id.
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
                db.commit()
            db.refresh(cs)
            return _session_to_state(cs, []), True
        finally:
            db.close()

    async def get_or_create_session_async(self) -> tuple[SessionState, bool]:
        """Async peer of ``get_or_create_session``.

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
        channel: str = "",
    ) -> StoredMessage:
        """Insert a message into the database and update the in-memory session."""
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session.session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                # Auto-create the session row (supports in-memory-only SessionState
                # objects created outside of get_or_create_session).
                now = datetime.datetime.now(datetime.UTC)
                cs = ChatSession(
                    session_id=session.session_id,
                    user_id=session.user_id,
                    channel=channel or session.channel,
                    created_at=now,
                    last_message_at=now,
                )
                db.add(cs)
                db.flush()

            # Lock the session row to serialize concurrent message inserts,
            # then calculate next seq. FOR UPDATE cannot be used with aggregates
            # in PostgreSQL, so we lock the parent row instead.
            db.execute(_select_session_for_update(cs.id)).scalar_one_or_none()
            max_seq: int = db.execute(_select_max_seq(cs.id)).scalar() or 0
            seq = max_seq + 1
            now = datetime.datetime.now(datetime.UTC)

            msg = Message(
                session_id=cs.id,
                seq=seq,
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now,
            )
            db.add(msg)

            # Update session metadata
            cs.last_message_at = now
            if channel:
                cs.channel = channel

            db.commit()

            # Update in-memory state
            stored = StoredMessage(
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
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

    async def add_message_async(
        self,
        session: SessionState,
        direction: str,
        body: str,
        external_message_id: str = "",
        media_urls_json: str = "[]",
        processed_context: str = "",
        tool_interactions_json: str = "",
        channel: str = "",
    ) -> StoredMessage:
        """Async peer of ``add_message``."""
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
        channel: str = "",
    ) -> StoredMessage:
        """Insert a message using only the session_id (no SessionState needed).

        Useful when the caller does not have a live ``SessionState`` object,
        e.g. persisting an approval prompt from the agent loop.
        """
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                raise ValueError(f"Session {session_id!r} not found for user {self.user_id!r}")

            db.execute(_select_session_for_update(cs.id)).scalar_one_or_none()
            max_seq: int = db.execute(_select_max_seq(cs.id)).scalar() or 0
            seq = max_seq + 1
            now = datetime.datetime.now(datetime.UTC)

            msg = Message(
                session_id=cs.id,
                seq=seq,
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now,
            )
            db.add(msg)
            cs.last_message_at = now
            if channel:
                cs.channel = channel
            db.commit()

            return StoredMessage(
                direction=direction,
                body=body,
                processed_context=processed_context,
                tool_interactions_json=tool_interactions_json,
                external_message_id=external_message_id,
                media_urls_json=media_urls_json,
                timestamp=now.isoformat(),
                seq=seq,
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
        channel: str = "",
    ) -> StoredMessage:
        """Async peer of ``add_message_by_session_id``."""
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
        """Update a message by seq number."""
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session.session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                return

            msg = db.execute(_select_message_by_seq(cs.id, seq)).scalar_one_or_none()
            if msg is None:
                return

            for key, value in updates.items():
                if key in _MESSAGE_UPDATABLE_FIELDS:
                    setattr(msg, key, value)
            db.commit()

            # Update in-memory
            for m in session.messages:
                if m.seq == seq:
                    for k, v in updates.items():
                        if k in _MESSAGE_UPDATABLE_FIELDS and hasattr(m, k):
                            setattr(m, k, v)
                    break

    async def update_message_async(
        self,
        session: SessionState,
        seq: int,
        **updates: Any,
    ) -> None:
        """Async peer of ``update_message``."""
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
        """Store the system prompt on the session if not already set."""
        if session.initial_system_prompt:
            return
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session.session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is not None and not cs.initial_system_prompt:
                cs.initial_system_prompt = system_prompt
                db.commit()
            session.initial_system_prompt = system_prompt

    async def update_initial_system_prompt_async(
        self, session: SessionState, system_prompt: str
    ) -> None:
        """Async peer of ``update_initial_system_prompt``."""
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

    def delete_message(self, session_id: str, seq: int) -> bool:
        """Delete a single message by seq number from a session.

        Returns True if a message was deleted, False if not found.
        """
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                return False
            count: int = cast(
                "CursorResult[object]",
                db.execute(_delete_message_by_seq(cs.id, seq)),
            ).rowcount
            db.commit()
            return count > 0

    async def delete_message_async(self, session_id: str, seq: int) -> bool:
        """Async peer of ``delete_message``."""
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return False
            result = await db.execute(_delete_message_by_seq(cs.id, seq))
            count: int = cast("CursorResult[object]", result).rowcount
            await db.commit()
            return count > 0

    # ------------------------------------------------------------------
    # delete_messages_by_seqs
    # ------------------------------------------------------------------

    def delete_messages_by_seqs(self, session_id: str, seqs: list[int]) -> int:
        """Delete specific messages by seq numbers from a session.

        Returns the number of messages actually deleted.
        """
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                return 0
            count: int = cast(
                "CursorResult[object]",
                db.execute(_delete_messages_by_seqs(cs.id, seqs)),
            ).rowcount
            db.commit()
            return count

    async def delete_messages_by_seqs_async(self, session_id: str, seqs: list[int]) -> int:
        """Async peer of ``delete_messages_by_seqs``."""
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return 0
            result = await db.execute(_delete_messages_by_seqs(cs.id, seqs))
            count: int = cast("CursorResult[object]", result).rowcount
            await db.commit()
            return count

    # ------------------------------------------------------------------
    # delete_messages
    # ------------------------------------------------------------------

    def delete_messages(self, session_id: str) -> int:
        """Delete all messages from a session and clear its initial system prompt.

        Returns the number of messages deleted. The session row itself is
        preserved so the conversation can continue with an empty history.
        """
        with db_session() as db:
            cs = db.execute(
                _select_session_by_session_id(session_id, self.user_id)
            ).scalar_one_or_none()
            if cs is None:
                return 0
            count: int = cast(
                "CursorResult[object]",
                db.execute(_delete_all_messages_for_session(cs.id)),
            ).rowcount
            cs.initial_system_prompt = ""
            db.commit()
            return count

    async def delete_messages_async(self, session_id: str) -> int:
        """Async peer of ``delete_messages``."""
        async with db_session_async() as db:
            cs = (
                await db.execute(_select_session_by_session_id(session_id, self.user_id))
            ).scalar_one_or_none()
            if cs is None:
                return 0
            result = await db.execute(_delete_all_messages_for_session(cs.id))
            count: int = cast("CursorResult[object]", result).rowcount
            cs.initial_system_prompt = ""
            await db.commit()
            return count

    # ------------------------------------------------------------------
    # last-timestamp helpers
    # ------------------------------------------------------------------

    def _get_last_timestamp(self, direction: str) -> datetime.datetime | None:
        """Get the most recent message timestamp in the given direction."""
        db = SessionLocal()
        try:
            return db.execute(_select_last_timestamp(self.user_id, direction)).scalar()
        finally:
            db.close()

    async def _get_last_timestamp_async(self, direction: str) -> datetime.datetime | None:
        """Async peer of ``_get_last_timestamp``."""
        db = AsyncSessionLocal()
        try:
            return (await db.execute(_select_last_timestamp(self.user_id, direction))).scalar()
        finally:
            await db.close()

    def get_last_inbound_timestamp(self) -> datetime.datetime | None:
        """Get the most recent inbound message timestamp."""
        return self._get_last_timestamp("inbound")

    async def get_last_inbound_timestamp_async(self) -> datetime.datetime | None:
        """Async peer of ``get_last_inbound_timestamp``."""
        return await self._get_last_timestamp_async("inbound")

    def get_last_outbound_timestamp(self) -> datetime.datetime | None:
        """Get the most recent outbound message timestamp."""
        return self._get_last_timestamp("outbound")

    async def get_last_outbound_timestamp_async(self) -> datetime.datetime | None:
        """Async peer of ``get_last_outbound_timestamp``."""
        return await self._get_last_timestamp_async("outbound")

    # ------------------------------------------------------------------
    # recent-message collectors
    # ------------------------------------------------------------------

    def _collect_messages(
        self,
        count: int | None = None,
        exclude_session_id: str | None = None,
    ) -> list[StoredMessage]:
        """Collect the most recent messages, optionally excluding a session."""
        resolved = count if count is not None else settings.heartbeat_recent_messages_count
        db = SessionLocal()
        try:
            messages = list(
                db.execute(_select_recent_messages(self.user_id, resolved, exclude_session_id))
                .scalars()
                .all()
            )
            # Return in chronological order
            return [_msg_to_stored(m) for m in reversed(messages)]
        finally:
            db.close()

    async def _collect_messages_async(
        self,
        count: int | None = None,
        exclude_session_id: str | None = None,
    ) -> list[StoredMessage]:
        """Async peer of ``_collect_messages``."""
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
            return [_msg_to_stored(m) for m in reversed(messages)]
        finally:
            await db.close()

    def get_recent_messages(self, count: int | None = None) -> list[StoredMessage]:
        """Get the most recent messages across all sessions."""
        return self._collect_messages(count)

    async def get_recent_messages_async(self, count: int | None = None) -> list[StoredMessage]:
        """Async peer of ``get_recent_messages``."""
        return await self._collect_messages_async(count)

    def get_other_session_messages(
        self,
        exclude_session_id: str,
        count: int | None = None,
    ) -> list[StoredMessage]:
        """Get recent messages from sessions other than *exclude_session_id*."""
        return self._collect_messages(count, exclude_session_id)

    async def get_other_session_messages_async(
        self,
        exclude_session_id: str,
        count: int | None = None,
    ) -> list[StoredMessage]:
        """Async peer of ``get_other_session_messages``."""
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
