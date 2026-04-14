"""Database-backed session store.

Replaces FileSessionStore from file_store.py. Uses ChatSession and Message
ORM models for persistence, while keeping SessionState and StoredMessage
Pydantic models as in-memory DTOs.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.agent.store_cache import StoreCache
from backend.app.config import settings
from backend.app.database import SessionLocal, db_session
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
        is_active=cs.is_active,
        created_at=cs.created_at.isoformat() if cs.created_at else "",
        last_message_at=cs.last_message_at.isoformat() if cs.last_message_at else "",
        last_compacted_seq=cs.last_compacted_seq,
        channel=cs.channel,
        initial_system_prompt=cs.initial_system_prompt,
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
    """Database-backed session storage using ChatSession and Message ORM models."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def load_session(self, session_id: str) -> SessionState | None:
        """Load a session by its string session_id."""
        db = SessionLocal()
        try:
            cs = (
                db.query(ChatSession).filter_by(session_id=session_id, user_id=self.user_id).first()
            )
            if cs is None:
                return None
            messages = db.query(Message).filter_by(session_id=cs.id).order_by(Message.seq).all()
            return _session_to_state(cs, messages)
        finally:
            db.close()

    def list_session_ids(self) -> list[str]:
        """List all session IDs for this user, sorted chronologically."""
        db = SessionLocal()
        try:
            rows = (
                db.query(ChatSession.session_id)
                .filter_by(user_id=self.user_id)
                .order_by(ChatSession.created_at)
                .all()
            )
            return [r[0] for r in rows]
        finally:
            db.close()

    async def list_sessions(self) -> list[SessionState]:
        """Return all sessions with their messages for this user."""
        db = SessionLocal()
        try:
            sessions = (
                db.query(ChatSession)
                .filter_by(user_id=self.user_id)
                .order_by(ChatSession.created_at)
                .all()
            )
            result = []
            for cs in sessions:
                messages = db.query(Message).filter_by(session_id=cs.id).order_by(Message.seq).all()
                result.append(_session_to_state(cs, messages))
            return result
        finally:
            db.close()

    async def get_or_create_session(
        self,
        force_new: bool = False,
    ) -> tuple[SessionState, bool]:
        """Get active session or create new one. Returns (session, is_new).

        Sessions are persistent: the most recent active session is always
        reused regardless of age. Pass ``force_new=True`` to explicitly
        start a new conversation.
        """
        db = SessionLocal()
        try:
            if not force_new:
                cs = (
                    db.query(ChatSession)
                    .filter_by(user_id=self.user_id, is_active=True)
                    .order_by(ChatSession.created_at.desc())
                    .first()
                )
                if cs is not None:
                    now = datetime.datetime.now(datetime.UTC)
                    cs.last_message_at = now
                    db.commit()
                    messages = (
                        db.query(Message).filter_by(session_id=cs.id).order_by(Message.seq).all()
                    )
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
                is_active=True,
                channel="",
                last_compacted_seq=0,
                created_at=now,
                last_message_at=now,
            )
            db.add(cs)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                # Extremely unlikely collision; retry with a new UUID
                short_uid = uuid.uuid4().hex[:8]
                session_id = f"{self.user_id}_{ts}_{short_uid}"
                cs = ChatSession(
                    session_id=session_id,
                    user_id=self.user_id,
                    is_active=True,
                    channel="",
                    last_compacted_seq=0,
                    created_at=now,
                    last_message_at=now,
                )
                db.add(cs)
                db.commit()
            db.refresh(cs)
            return _session_to_state(cs, []), True
        finally:
            db.close()

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
            cs = (
                db.query(ChatSession)
                .filter_by(session_id=session.session_id, user_id=self.user_id)
                .first()
            )
            if cs is None:
                # Auto-create the session row (supports in-memory-only SessionState
                # objects created outside of get_or_create_session).
                now = datetime.datetime.now(datetime.UTC)
                cs = ChatSession(
                    session_id=session.session_id,
                    user_id=session.user_id,
                    is_active=session.is_active,
                    channel=channel or session.channel,
                    last_compacted_seq=session.last_compacted_seq,
                    created_at=now,
                    last_message_at=now,
                )
                db.add(cs)
                db.flush()

            # Lock the session row to serialize concurrent message inserts,
            # then calculate next seq. FOR UPDATE cannot be used with aggregates
            # in PostgreSQL, so we lock the parent row instead.
            db.query(ChatSession).filter_by(id=cs.id).with_for_update().first()
            max_seq: int = (
                db.query(func.max(Message.seq)).filter_by(session_id=cs.id).scalar()
            ) or 0
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
            cs = (
                db.query(ChatSession).filter_by(session_id=session_id, user_id=self.user_id).first()
            )
            if cs is None:
                raise ValueError(f"Session {session_id!r} not found for user {self.user_id!r}")

            db.query(ChatSession).filter_by(id=cs.id).with_for_update().first()
            max_seq: int = (
                db.query(func.max(Message.seq)).filter_by(session_id=cs.id).scalar()
            ) or 0
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

    async def update_message(
        self,
        session: SessionState,
        seq: int,
        **updates: Any,
    ) -> None:
        """Update a message by seq number."""
        with db_session() as db:
            cs = (
                db.query(ChatSession)
                .filter_by(session_id=session.session_id, user_id=self.user_id)
                .first()
            )
            if cs is None:
                return

            msg = db.query(Message).filter_by(session_id=cs.id, seq=seq).first()
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

    async def update_initial_system_prompt(self, session: SessionState, system_prompt: str) -> None:
        """Store the system prompt on the session if not already set."""
        if session.initial_system_prompt:
            return
        with db_session() as db:
            cs = (
                db.query(ChatSession)
                .filter_by(session_id=session.session_id, user_id=self.user_id)
                .first()
            )
            if cs is not None and not cs.initial_system_prompt:
                cs.initial_system_prompt = system_prompt
                db.commit()
            session.initial_system_prompt = system_prompt

    async def update_compaction_seq(self, session: SessionState, seq: int) -> None:
        """Update the last_compacted_seq in session metadata."""
        with db_session() as db:
            cs = (
                db.query(ChatSession)
                .filter_by(session_id=session.session_id, user_id=self.user_id)
                .first()
            )
            if cs is not None:
                cs.last_compacted_seq = seq
                db.commit()
            session.last_compacted_seq = seq

    def delete_message(self, session_id: str, seq: int) -> bool:
        """Delete a single message by seq number from a session.

        Returns True if a message was deleted, False if not found.
        """
        with db_session() as db:
            cs = (
                db.query(ChatSession).filter_by(session_id=session_id, user_id=self.user_id).first()
            )
            if cs is None:
                return False
            count: int = (
                db.query(Message)
                .filter_by(session_id=cs.id, seq=seq)
                .delete(synchronize_session="fetch")
            )
            db.commit()
            return count > 0

    def delete_messages(self, session_id: str) -> int:
        """Delete all messages from a session and reset its compaction pointer.

        Returns the number of messages deleted. The session row itself is
        preserved so the conversation can continue with an empty history.
        """
        with db_session() as db:
            cs = (
                db.query(ChatSession).filter_by(session_id=session_id, user_id=self.user_id).first()
            )
            if cs is None:
                return 0
            count: int = (
                db.query(Message)
                .filter_by(session_id=cs.id)
                .delete(
                    synchronize_session="fetch",
                )
            )
            cs.last_compacted_seq = 0
            cs.initial_system_prompt = ""
            db.commit()
            return count

    def _get_last_timestamp(self, direction: str) -> datetime.datetime | None:
        """Get the most recent message timestamp in the given direction."""
        db = SessionLocal()
        try:
            result = (
                db.query(func.max(Message.timestamp))
                .join(ChatSession, Message.session_id == ChatSession.id)
                .filter(ChatSession.user_id == self.user_id, Message.direction == direction)
                .scalar()
            )
            return result
        finally:
            db.close()

    def get_last_inbound_timestamp(self) -> datetime.datetime | None:
        """Get the most recent inbound message timestamp."""
        return self._get_last_timestamp("inbound")

    def get_last_outbound_timestamp(self) -> datetime.datetime | None:
        """Get the most recent outbound message timestamp."""
        return self._get_last_timestamp("outbound")

    def _collect_messages(
        self,
        count: int | None = None,
        exclude_session_id: str | None = None,
    ) -> list[StoredMessage]:
        """Collect the most recent messages, optionally excluding a session."""
        count = count if count is not None else settings.heartbeat_recent_messages_count
        db = SessionLocal()
        try:
            query = (
                db.query(Message)
                .join(ChatSession, Message.session_id == ChatSession.id)
                .filter(ChatSession.user_id == self.user_id)
            )
            if exclude_session_id:
                query = query.filter(ChatSession.session_id != exclude_session_id)

            messages = query.order_by(Message.timestamp.desc()).limit(count).all()
            # Return in chronological order
            return [_msg_to_stored(m) for m in reversed(messages)]
        finally:
            db.close()

    def get_recent_messages(self, count: int | None = None) -> list[StoredMessage]:
        """Get the most recent messages across all sessions."""
        return self._collect_messages(count)

    def get_other_session_messages(
        self,
        exclude_session_id: str,
        count: int | None = None,
    ) -> list[StoredMessage]:
        """Get recent messages from sessions other than *exclude_session_id*."""
        return self._collect_messages(count, exclude_session_id)


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
