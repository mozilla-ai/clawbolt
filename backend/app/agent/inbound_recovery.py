"""Startup recovery for orphaned inbound messages.

The ingestion path persists every inbound to ``messages`` *before* the
``MessageBatcher`` schedules an in-memory flush timer (1.5 s by default).
When a worker dies during that window (deploy, OOM, crash), the timer
task is lost. The message is durable in the DB but the pipeline never
runs, so the user gets silence: no agent reply, no outbound, just a
message sitting in history that the next inbound proceeds past.

We saw this in production: a contractor's "Can you change my calendar
reminders to my work iCloud?" landed in the DB at 2026-04-30 15:01 UTC
but received no reply for 29 hours, until the next message woke a fresh
batcher state.

This module sweeps for those orphans on app startup. The sweep is
narrowly scoped:

- Only inbound messages from the last ``inbound_recovery_lookback_minutes``
  (default 30) are considered. Older orphans are unlikely to still be
  relevant to the user and re-dispatching would produce a stale reply.
- A message is "orphaned" when there is no outbound ``message`` in the
  same session with a higher seq. Outbound is the only structural
  signal that the agent loop ran for that inbound.
- We re-dispatch through ``_dispatch_to_pipeline`` rather than the bus,
  so ``add_message`` does not duplicate the persisted row. The agent
  loop sees the same session state it would have seen at the original
  dispatch.

The mechanism is structurally similar to ``cleanup_orphaned_approvals``:
both run in ``lifespan`` after channels start, both are best-effort, and
both log every recovery decision so an operator can see what fired on
the boot after an incident.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import and_, exists

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _find_orphaned_inbounds(
    db: Session,
    cutoff_utc: datetime.datetime,
) -> list[tuple[Message, ChatSession]]:
    """Return (message, session) pairs for inbound rows with no outbound after.

    "Outbound after" means a Message in the same session with strictly
    higher seq and direction='outbound'. The seq comparison handles the
    case where a heartbeat or compaction event might have raced past
    the inbound and flushed a follow-on message: any outbound at all
    after the inbound is enough to declare it processed.
    """
    OutboundAfter = Message.__table__.alias("ob")
    has_outbound_after = exists().where(
        and_(
            OutboundAfter.c.session_id == Message.session_id,
            OutboundAfter.c.seq > Message.seq,
            OutboundAfter.c.direction == MessageDirection.OUTBOUND,
        )
    )
    rows = (
        db.query(Message, ChatSession)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .filter(
            Message.direction == MessageDirection.INBOUND,
            Message.timestamp >= cutoff_utc,
            ~has_outbound_after,
        )
        .order_by(Message.timestamp.asc())
        .all()
    )
    # Convert Row objects to plain tuples so callers don't need to know
    # the Row API.
    return [(row[0], row[1]) for row in rows]


def _build_dispatch_inputs(
    db: Session,
    msg: Message,
    chat_session: ChatSession,
) -> tuple[User, SessionState, StoredMessage] | None:
    """Reconstruct the (user, session_state, stored_message) tuple needed by
    ``_dispatch_to_pipeline``. Returns None when the user row is missing
    (extremely unlikely outside of a manual delete during the window)."""
    user = db.query(User).filter_by(id=chat_session.user_id).first()
    if user is None:
        logger.warning(
            "Skipping orphan recovery for message %d: user %s not found",
            msg.id,
            chat_session.user_id,
        )
        return None
    db.expunge(user)

    stored = StoredMessage(
        direction=msg.direction,
        body=msg.body or "",
        processed_context=msg.processed_context or "",
        tool_interactions_json=msg.tool_interactions_json or "",
        external_message_id=msg.external_message_id or "",
        media_urls_json=msg.media_urls_json or "[]",
        timestamp=msg.timestamp.isoformat() if msg.timestamp else "",
        seq=msg.seq,
    )

    state = SessionState(
        session_id=chat_session.session_id,
        user_id=chat_session.user_id,
        messages=[stored],
        is_active=chat_session.is_active,
        created_at=chat_session.created_at.isoformat() if chat_session.created_at else "",
        last_message_at=(
            chat_session.last_message_at.isoformat() if chat_session.last_message_at else ""
        ),
        channel=chat_session.channel or "",
        last_compacted_seq=chat_session.last_compacted_seq,
    )
    return user, state, stored


def _parse_media_refs(media_urls_json: str) -> list[tuple[str, str]]:
    """Reconstruct ``media_refs`` from the persisted JSON list of file ids.

    The original ``InboundMessage.media_refs`` is ``list[tuple[str, str]]``
    (file_id, mime_type). The DB only persists the file_ids list, so the
    mime_type is unrecoverable. Empty string is the safe fallback: the
    media pipeline re-derives mime types from the stored file when needed.
    """
    if not media_urls_json:
        return []
    try:
        ids = json.loads(media_urls_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(ids, list):
        return []
    return [(str(fid), "") for fid in ids]


async def recover_orphan_inbound_messages() -> int:
    """Re-dispatch any inbound messages that look like they never ran.

    Called from ``lifespan`` after channels start. Returns the number of
    messages re-dispatched, mostly for the startup log line. Best-effort:
    the sweep itself is wrapped in try/except by the caller so a recovery
    bug never blocks app startup.
    """
    # Imported here to avoid a circular import: ingestion imports from this
    # module's siblings, and we only need the dispatch function at call time.
    from backend.app.agent.ingestion import _dispatch_to_pipeline

    lookback_minutes = settings.inbound_recovery_lookback_minutes
    if lookback_minutes == 0:
        logger.debug("Inbound recovery disabled (inbound_recovery_lookback_minutes=0)")
        return 0

    lookback = datetime.timedelta(minutes=lookback_minutes)
    cutoff = datetime.datetime.now(datetime.UTC) - lookback

    db = SessionLocal()
    try:
        rows = _find_orphaned_inbounds(db, cutoff)
        if not rows:
            return 0

        logger.info(
            "Inbound recovery: found %d orphan(s) since %s",
            len(rows),
            cutoff.isoformat(),
        )

        dispatched = 0
        for msg, chat_session in rows:
            inputs = _build_dispatch_inputs(db, msg, chat_session)
            if inputs is None:
                continue
            user, state, stored = inputs
            channel = chat_session.channel or ""
            media_refs = _parse_media_refs(msg.media_urls_json or "")
            try:
                # Re-run the original conversation lookup so any session
                # rotation that happened since the message was written is
                # honored. Falls back to the reconstructed state above if
                # this raises.
                refreshed_state, _ = await get_or_create_conversation(
                    user.id, external_session_id=chat_session.session_id
                )
                state = refreshed_state if refreshed_state is not None else state
            except Exception:
                logger.exception(
                    "get_or_create_conversation failed during inbound recovery for user %s",
                    user.id,
                )

            logger.info(
                "Re-dispatching orphan inbound seq=%d session=%s user=%s",
                msg.seq,
                chat_session.session_id,
                user.id,
            )
            try:
                await _dispatch_to_pipeline(
                    user=user,
                    session=state,
                    message=stored,
                    media_urls=media_refs,
                    channel=channel,
                    request_id="",
                    downloaded_media=None,
                    download_media=None,
                )
                dispatched += 1
            except Exception:
                logger.exception(
                    "Failed to re-dispatch orphan inbound seq=%d (session %s, user %s)",
                    msg.seq,
                    chat_session.session_id,
                    user.id,
                )

        return dispatched
    finally:
        db.close()


__all__ = [
    "recover_orphan_inbound_messages",
]
