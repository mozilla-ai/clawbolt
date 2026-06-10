"""Startup retry for compaction events stuck in ``'pending'``.

``trigger_compaction_for_dropped`` advances the trim watermark
synchronously, then runs the compaction LLM call as a fire-and-forget
background task. When the process dies mid-call (deploy restart, OOM) or
the call fails (provider outage), the ``CompactionEvent`` row stays
``'pending'`` forever: the watermark is already advanced, so the seq
range never reaches the LLM again, and the facts in it were never
extracted into MEMORY.md / USER.md / SOUL.md.

Everything needed to recover is durable: the row records the seq range
(``min_message_seq`` / ``max_message_seq``) and message rows are never
deleted. This module sweeps for stale pending rows on app startup and
re-runs ``compact_session`` against the original range, mirroring the
``inbound_recovery`` sweep (per-process advisory lock, lookback window,
best-effort semantics, one worker per rolling restart).

Bounds, so the sweep cannot loop or race:

- Only rows older than ``_GRACE_SECONDS`` are considered, so the sweep
  never races a compaction task that is legitimately still in flight.
- Only rows younger than ``compaction_retry_lookback_minutes`` are
  considered; ``0`` disables the sweep entirely.
- Each attempt increments ``retry_count`` *before* the LLM call, so a
  crash mid-retry still counts the attempt. Rows at ``_MAX_ATTEMPTS``
  stop being selected: a poisoned range cannot retry forever. They stay
  ``'pending'`` so an admin can still find them (the conversation that
  should have been compacted is recoverable via the seq range).
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from sqlalchemy import Select, select, text

from backend.app.agent.compaction import compact_session
from backend.app.agent.context import _stored_messages_to_agent_messages
from backend.app.agent.session_db import _msg_to_stored
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, db_session_async, get_async_engine
from backend.app.models import ChatSession, CompactionEvent, Message, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# Postgres advisory lock key; one worker runs the sweep per rolling
# restart. Same shape as ``inbound_recovery._RECOVERY_LOCK_KEY``.
_RECOVERY_LOCK_KEY = "compaction_recovery:cleanup"

# Do not retry rows younger than this. A compaction LLM call that is
# still legitimately in flight (slow provider, long input) must not be
# raced by the sweep; 10 minutes comfortably exceeds any sane call.
_GRACE_SECONDS = 600

# Attempts per row across all boots. At the cap the row stays
# ``'pending'`` but stops being selected.
_MAX_ATTEMPTS = 3


def _stale_pending_events_select(
    cutoff_utc: datetime.datetime,
    grace_floor_utc: datetime.datetime,
) -> Select[tuple[CompactionEvent]]:
    """Pure builder for the stale-pending-event query.

    Rows must carry a seq range: legacy rows (pre-watermark feature) have
    NULL ``min_message_seq`` and cannot be replayed deterministically.
    """
    return (
        select(CompactionEvent)
        .where(
            CompactionEvent.status == "pending",
            CompactionEvent.triggered_at >= cutoff_utc,
            CompactionEvent.triggered_at <= grace_floor_utc,
            CompactionEvent.retry_count < _MAX_ATTEMPTS,
            CompactionEvent.min_message_seq.is_not(None),
            CompactionEvent.max_message_seq.is_not(None),
        )
        .order_by(CompactionEvent.triggered_at.asc())
    )


def _messages_in_range_select(cs_id: int, min_seq: int, max_seq: int) -> Select[tuple[Message]]:
    """Messages covered by a pending event's recorded seq range."""
    return (
        select(Message)
        .where(
            Message.session_id == cs_id,
            Message.seq >= min_seq,
            Message.seq <= max_seq,
        )
        .order_by(Message.seq)
    )


async def _try_acquire_lock_async(conn: AsyncConnection) -> bool:
    """Acquire the per-process sweep lock on a dedicated connection.

    Must be an ``AsyncConnection``, not an ``AsyncSession``: the
    session-scoped advisory lock has to stay pinned to one physical
    Postgres session from acquire through unlock. See the equivalent
    helper in ``backend/app/agent/inbound_recovery.py`` for the full
    rationale.
    """
    try:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        )
        got_lock = result.scalar()
        await conn.commit()
    except Exception:
        logger.exception("Failed to acquire compaction-recovery advisory lock")
        return False
    return bool(got_lock)


async def _release_lock_async(conn: AsyncConnection) -> None:
    """Best-effort release; must run on the connection that took the lock."""
    try:
        await conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:k))"),
            {"k": _RECOVERY_LOCK_KEY},
        )
        await conn.commit()
    except Exception:
        logger.exception("Failed to release compaction-recovery advisory lock")


async def _claim_attempt(event_id: int) -> bool:
    """Increment ``retry_count`` under a row lock; False when not claimable.

    Runs in its own transaction *before* the LLM call so a crash
    mid-retry still consumes the attempt. Re-checks status and the
    attempt cap under the lock so two sweeps (or a sweep racing the
    original background task's late completion) cannot double-claim.
    """
    async with db_session_async() as db:
        ev = (
            await db.execute(select(CompactionEvent).filter_by(id=event_id).with_for_update())
        ).scalar_one_or_none()
        if ev is None or ev.status != "pending" or ev.retry_count >= _MAX_ATTEMPTS:
            return False
        ev.retry_count += 1
        await db.commit()
        return True


async def _exhaust_event(event_id: int) -> None:
    """Stop retrying a row whose seq range has nothing left to compact."""
    async with db_session_async() as db:
        ev = (
            await db.execute(select(CompactionEvent).filter_by(id=event_id).with_for_update())
        ).scalar_one_or_none()
        if ev is not None and ev.status == "pending":
            ev.retry_count = _MAX_ATTEMPTS
            await db.commit()


async def _event_completed(event_id: int) -> bool:
    """Read back whether ``compact_session`` flipped the row."""
    db = AsyncSessionLocal()
    try:
        status = (
            await db.execute(select(CompactionEvent.status).filter_by(id=event_id))
        ).scalar_one_or_none()
        return status == "completed"
    finally:
        await db.close()


async def _retry_event(
    event_id: int,
    user_id: str,
    min_seq: int,
    max_seq: int,
) -> bool:
    """Re-run one pending event; True when the row flipped to completed."""
    if not await _claim_attempt(event_id):
        return False

    db = AsyncSessionLocal()
    try:
        cs = (await db.execute(select(ChatSession).filter_by(user_id=user_id))).scalar_one_or_none()
        user = (await db.execute(select(User).filter_by(id=user_id))).scalar_one_or_none()
        rows = (
            list(
                (await db.execute(_messages_in_range_select(cs.id, min_seq, max_seq)))
                .scalars()
                .all()
            )
            if cs is not None
            else []
        )
    finally:
        await db.close()

    tz_name = user.timezone if user is not None else ""
    stored = [_msg_to_stored(m) for m in rows]
    agent_messages = _stored_messages_to_agent_messages(stored, tz_name=tz_name)
    if not agent_messages:
        # The session or the rows were deleted since the event was
        # written, or every row in the range is filtered (approval
        # prompts, blank placeholders). Nothing to extract; stop
        # selecting the row.
        logger.info(
            "Compaction recovery: event id=%d user=%s seq=[%d,%d] has no "
            "recoverable messages; exhausting",
            event_id,
            user_id,
            min_seq,
            max_seq,
        )
        await _exhaust_event(event_id)
        return False

    logger.info(
        "Retrying pending compaction event id=%d user=%s seq=[%d,%d] (%d message(s))",
        event_id,
        user_id,
        min_seq,
        max_seq,
        len(agent_messages),
    )
    # ``compact_session`` flips the row to 'completed' on success
    # (including the nothing-changed case). On LLM failure it logs and
    # returns early, leaving the row 'pending' for the next boot,
    # bounded by the retry_count cap.
    await compact_session(
        user_id,
        agent_messages,
        max_message_seq=max_seq,
        event_id=event_id,
    )
    return await _event_completed(event_id)


async def recover_pending_compactions() -> int:
    """Retry compaction events stuck in ``'pending'``.

    Called from ``lifespan`` after channels start. Returns the number of
    events that flipped to ``'completed'``, for the startup log line.
    Best-effort: the caller wraps the sweep in try/except so a recovery
    bug never blocks app startup.
    """
    lookback_minutes = settings.compaction_retry_lookback_minutes
    if lookback_minutes == 0 or not settings.compaction_enabled:
        logger.debug("Compaction recovery disabled")
        return 0

    now = datetime.datetime.now(datetime.UTC)
    cutoff = now - datetime.timedelta(minutes=lookback_minutes)
    grace_floor = now - datetime.timedelta(seconds=_GRACE_SECONDS)

    lock_conn = await get_async_engine().connect()
    lock_acquired = False
    try:
        if not await _try_acquire_lock_async(lock_conn):
            logger.info("Another worker is running compaction recovery; skipping on this boot")
            return 0
        lock_acquired = True

        db = AsyncSessionLocal()
        try:
            events = [
                (ev.id, ev.user_id, ev.min_message_seq, ev.max_message_seq)
                for ev in (
                    (await db.execute(_stale_pending_events_select(cutoff, grace_floor)))
                    .scalars()
                    .all()
                )
            ]
        finally:
            await db.close()

        if not events:
            return 0

        logger.info(
            "Compaction recovery: found %d stale pending event(s) between %s and %s",
            len(events),
            cutoff.isoformat(),
            grace_floor.isoformat(),
        )

        completed = 0
        for event_id, user_id, min_seq, max_seq in events:
            if event_id is None or min_seq is None or max_seq is None:
                continue
            try:
                if await _retry_event(event_id, user_id, min_seq, max_seq):
                    completed += 1
            except Exception:
                logger.exception(
                    "Compaction retry failed for event id=%d user=%s",
                    event_id,
                    user_id,
                )
        return completed
    finally:
        if lock_acquired:
            await _release_lock_async(lock_conn)
        await lock_conn.close()
