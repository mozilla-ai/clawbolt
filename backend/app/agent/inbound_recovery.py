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
- Only inbounds at least ``_FRESHNESS_FLOOR_SECONDS`` old are considered,
  so a NEW inbound arriving on a freshly-started worker (concurrent with
  this sweep) doesn't get racing dispatched once by the normal ingestion
  path and again by recovery.
- A message is "orphaned" when there is no outbound ``message`` in the
  same session with a higher seq. Outbound is the only structural
  signal that the agent loop ran for that inbound.
- We re-dispatch through ``_dispatch_to_pipeline`` rather than the bus,
  so ``add_message`` does not duplicate the persisted row. The agent
  loop sees the same session state it would have seen at the original
  dispatch.
- A Postgres advisory lock (``inbound_recovery:cleanup``) ensures
  exactly one worker runs the sweep on a rolling restart, mirroring the
  pattern in ``cleanup_orphaned_approvals``.

Known limitation: a heartbeat or compaction outbound that landed AFTER
the orphan inbound but BEFORE the worker recovered will mask the orphan
(the EXISTS subquery sees that outbound and treats the inbound as
processed). This is structural to using "any later outbound" as the
signal. Fixing it would require either a column distinguishing
heartbeat-origin outbounds from agent-loop replies, or a
``responding_to_seq`` foreign key on outbound rows; both are larger
changes deferred until the heuristic produces a real false negative.

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

from sqlalchemy import Select, and_, exists, select, text

from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.agent.ingestion import _dispatch_to_pipeline
from backend.app.agent.session_db import get_session_store
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, get_async_engine
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Postgres advisory lock key. ``hashtext`` reduces the string to an int
# the lock function accepts. Same shape as ``approval._CLEANUP_LOCK_KEY``.
_RECOVERY_LOCK_KEY = "inbound_recovery:cleanup"

# Don't try to recover messages younger than this. The normal ingestion
# path is dispatching them right now (via the in-memory MessageBatcher
# timer), and a parallel recovery would race it. 5 s comfortably covers
# the 1.5 s batcher window plus pipeline acquisition latency.
_FRESHNESS_FLOOR_SECONDS = 5


def _orphaned_inbounds_select(
    cutoff_utc: datetime.datetime,
    freshness_floor_utc: datetime.datetime,
) -> Select[tuple[Message, ChatSession]]:
    """Pure builder for the orphan-inbound query.

    Shared between sync and async recovery paths so the query shape and
    filter semantics stay identical. See ``_find_orphaned_inbounds``
    for the rationale on each filter.
    """
    OutboundAfter = Message.__table__.alias("ob")
    has_outbound_after = exists().where(
        and_(
            OutboundAfter.c.session_id == Message.session_id,
            OutboundAfter.c.seq > Message.seq,
            OutboundAfter.c.direction == MessageDirection.OUTBOUND,
        )
    )
    return (
        select(Message, ChatSession)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(
            Message.direction == MessageDirection.INBOUND,
            Message.timestamp >= cutoff_utc,
            Message.timestamp <= freshness_floor_utc,
            ~has_outbound_after,
        )
        .order_by(Message.timestamp.asc())
    )


def _find_orphaned_inbounds(
    db: Session,
    cutoff_utc: datetime.datetime,
    freshness_floor_utc: datetime.datetime,
) -> list[tuple[Message, ChatSession]]:
    """Return (message, session) pairs for inbound rows with no outbound after.

    "Outbound after" means a Message in the same session with strictly
    higher seq and direction='outbound'. The seq comparison handles the
    case where a heartbeat or compaction event might have raced past
    the inbound: any outbound at all after the inbound is enough to
    declare it processed (with the documented heartbeat-masking
    limitation).

    *freshness_floor_utc* excludes messages newer than this timestamp so
    the sweep doesn't race the normal ingestion path on a brand-new
    inbound that arrived during the sweep itself.
    """
    rows = db.execute(_orphaned_inbounds_select(cutoff_utc, freshness_floor_utc)).all()
    # Convert Row objects to plain tuples so callers don't need to know
    # the Row API.
    return [(row[0], row[1]) for row in rows]


async def _find_orphaned_inbounds_async(
    db: AsyncSession,
    cutoff_utc: datetime.datetime,
    freshness_floor_utc: datetime.datetime,
) -> list[tuple[Message, ChatSession]]:
    """Async peer of ``_find_orphaned_inbounds``."""
    rows = (await db.execute(_orphaned_inbounds_select(cutoff_utc, freshness_floor_utc))).all()
    return [(row[0], row[1]) for row in rows]


def _select_user_by_id(user_id: str) -> Select[tuple[User]]:
    """Pure builder for the per-orphan user lookup.

    Shared between sync and async ``_build_dispatch_inputs`` peers so
    they cannot drift on filter shape.
    """
    return select(User).filter_by(id=user_id)


def _orphan_to_stored_and_state(
    msg: Message,
    chat_session: ChatSession,
) -> tuple[StoredMessage, SessionState]:
    """Pure mapping from an orphan ``Message`` row plus its
    ``ChatSession`` to the (stored_message, session_state) the dispatch
    pipeline expects.

    No DB access; safe to share between sync and async peers.
    """
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
        created_at=chat_session.created_at.isoformat() if chat_session.created_at else "",
        last_message_at=(
            chat_session.last_message_at.isoformat() if chat_session.last_message_at else ""
        ),
        channel=chat_session.channel or "",
    )
    return stored, state


def _build_dispatch_inputs(
    db: Session,
    msg: Message,
    chat_session: ChatSession,
) -> tuple[User, SessionState, StoredMessage] | None:
    """Reconstruct the (user, session_state, stored_message) tuple needed by
    ``_dispatch_to_pipeline``.

    The returned ``SessionState`` carries only the orphan message in its
    ``messages`` list. That is intentional: ``_dispatch_to_pipeline``
    re-loads the full session from the DB before invoking the agent loop
    (see ``ingestion.py:_dispatch_to_pipeline`` -> ``handle_inbound_message``
    -> ``router._build_message_context``). The single-message state here
    just carries the ``session_id`` so the dispatcher knows which session
    to load.

    Returns None when the user row is missing (extremely unlikely outside
    of a manual delete during the recovery window).
    """
    user = db.execute(_select_user_by_id(chat_session.user_id)).scalar_one_or_none()
    if user is None:
        logger.warning(
            "Skipping orphan recovery for message %d: user %s not found",
            msg.id,
            chat_session.user_id,
        )
        return None
    db.expunge(user)

    stored, state = _orphan_to_stored_and_state(msg, chat_session)
    return user, state, stored


async def _build_dispatch_inputs_async(
    db: AsyncSession,
    msg: Message,
    chat_session: ChatSession,
) -> tuple[User, SessionState, StoredMessage] | None:
    """Async peer of ``_build_dispatch_inputs``."""
    user = (await db.execute(_select_user_by_id(chat_session.user_id))).scalar_one_or_none()
    if user is None:
        logger.warning(
            "Skipping orphan recovery for message %d: user %s not found",
            msg.id,
            chat_session.user_id,
        )
        return None
    db.expunge(user)

    stored, state = _orphan_to_stored_and_state(msg, chat_session)
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


def _try_acquire_lock(db: Session) -> bool:
    """Acquire the per-process recovery lock, mirroring approval cleanup.

    ``pg_try_advisory_lock`` returns True on first acquisition, False if
    another connection (i.e. another worker on a rolling restart) holds
    the lock. We commit immediately to release the implicit read
    transaction; the advisory lock itself is session-scoped and survives.
    """
    try:
        got_lock = db.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        ).scalar()
        db.commit()
    except Exception:
        logger.exception("Failed to acquire inbound-recovery advisory lock")
        return False
    return bool(got_lock)


def _release_lock(db: Session) -> None:
    """Best-effort release of the recovery lock."""
    try:
        db.execute(
            text("SELECT pg_advisory_unlock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        )
        db.commit()
    except Exception:
        logger.exception("Failed to release inbound-recovery advisory lock")


async def _try_acquire_lock_async(conn: AsyncConnection) -> bool:
    """Async peer of ``_try_acquire_lock``.

    ``conn`` MUST be an ``AsyncConnection`` (not an ``AsyncSession``):
    ``AsyncSession.commit()`` returns the underlying connection to the
    pool, which would let a peer task pick the same connection up and
    re-enter the critical section (locks are reentrant per PG session).
    The session-scoped advisory lock must stay pinned to one physical
    connection from acquire through unlock. The
    ``test_unlock_on_different_connection_is_a_no_op`` regression in
    ``tests/test_inbound_recovery.py`` traps any future refactor that
    breaks this coupling.
    """
    try:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        )
        got_lock = result.scalar()
        # Commit ends the implicit read transaction so we don't sit
        # idle-in-transaction while we go off and do recovery work. On
        # an AsyncConnection this does NOT return the connection to
        # the pool, so the advisory lock stays attached.
        await conn.commit()
    except Exception:
        logger.exception("Failed to acquire inbound-recovery advisory lock")
        return False
    return bool(got_lock)


async def _release_lock_async(conn: AsyncConnection) -> None:
    """Async peer of ``_release_lock``.

    Must run on the same ``AsyncConnection`` that took the lock or
    Postgres returns ``False`` and the unlock silently no-ops. See
    ``_try_acquire_lock_async`` for the connection-coupling rationale.
    """
    try:
        await conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        )
        await conn.commit()
    except Exception:
        logger.exception("Failed to release inbound-recovery advisory lock")


async def recover_orphan_inbound_messages() -> int:
    """Re-dispatch any inbound messages that look like they never ran.

    Called from ``lifespan`` after channels start. Returns the number of
    messages re-dispatched, mostly for the startup log line. Best-effort:
    the sweep itself is wrapped in try/except by the caller so a recovery
    bug never blocks app startup.

    The advisory lock and the per-orphan queries run on different
    handles. The lock connection (``AsyncConnection`` from
    ``get_async_engine``) is held across the whole sweep so the
    session-scoped ``pg_try_advisory_lock`` / ``pg_advisory_unlock``
    pair lands on the same physical Postgres session. The queries run
    on a separate ``AsyncSession`` so its commits do not bounce the
    lock connection. Pool sizing note: each in-flight recovery sweep
    holds one async-pool connection for the duration of the sweep
    (lock + dispatches), and ``_dispatch_to_pipeline`` itself may
    acquire more connections for the agent loop. Operators sizing the
    async pool should budget for this concurrency.
    """
    lookback_minutes = settings.inbound_recovery_lookback_minutes
    if lookback_minutes == 0:
        logger.debug("Inbound recovery disabled (inbound_recovery_lookback_minutes=0)")
        return 0

    now = datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(minutes=lookback_minutes)
    freshness_floor = now - datetime.timedelta(seconds=_FRESHNESS_FLOOR_SECONDS)

    lock_conn = await get_async_engine().connect()
    lock_acquired = False
    try:
        if not await _try_acquire_lock_async(lock_conn):
            logger.info("Another worker is running inbound recovery; skipping on this boot")
            return 0
        lock_acquired = True

        # The query session is independent of the lock connection so its
        # commit/close lifecycle does not disturb the advisory lock that
        # ``lock_conn`` holds across the whole sweep.
        db: AsyncSession = AsyncSessionLocal()
        try:
            rows = await _find_orphaned_inbounds_async(db, cutoff, freshness_floor)
            if not rows:
                return 0

            logger.info(
                "Inbound recovery: found %d orphan(s) between %s and %s",
                len(rows),
                cutoff.isoformat(),
                freshness_floor.isoformat(),
            )

            dispatched = 0
            for msg, chat_session in rows:
                inputs = await _build_dispatch_inputs_async(db, msg, chat_session)
                if inputs is None:
                    continue
                user, state, stored = inputs
                channel = chat_session.channel or ""
                media_refs = _parse_media_refs(msg.media_urls_json or "")
                try:
                    # Re-run the conversation lookup so the live session
                    # state reflects any messages persisted since the
                    # orphan was written. Falls back to the reconstructed
                    # state above if this raises. Use the async store
                    # directly so the recovery sweep stays fully async
                    # (the sync ``get_or_create_conversation`` would
                    # block the event loop on the DB call).
                    refreshed_state, _ = await get_session_store(
                        user.id
                    ).get_or_create_session_async()
                    state = refreshed_state if refreshed_state is not None else state
                except Exception:
                    logger.exception(
                        "get_or_create_session_async failed during inbound recovery for user %s",
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
            await db.close()
    finally:
        if lock_acquired:
            await _release_lock_async(lock_conn)
        await lock_conn.close()


__all__ = [
    "recover_orphan_inbound_messages",
]
