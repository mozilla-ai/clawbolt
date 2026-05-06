"""Tests for the orphan inbound startup recovery sweep.

The sweep finds inbound messages that landed in the DB but never produced
an outbound (worker died during the MessageBatcher window), and
re-dispatches each via ``_dispatch_to_pipeline``. These tests exercise:

* The orphan-detection SQL: an inbound with no following outbound is a
  candidate; an inbound followed by any outbound is not.
* The lookback window: messages older than the cutoff are ignored.
* The disable switch: ``inbound_recovery_lookback_minutes=0`` short-circuits.
* End-to-end dispatch: the recovery path calls ``_dispatch_to_pipeline``
  with the right arguments and counts successes correctly.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agent.inbound_recovery import (
    _build_dispatch_inputs_async,
    _find_orphaned_inbounds_async,
    _parse_media_refs,
    recover_orphan_inbound_messages,
)
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User


async def _make_user_async(factory: async_sessionmaker) -> User:
    """Async peer of ``_make_user`` for end-to-end tests that exercise
    ``recover_orphan_inbound_messages`` (which now runs on the async
    DB engine; a sync write would land in a different connection and
    not be visible to the async sweep)."""
    async with factory() as db:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"recovery-test-{uuid.uuid4().hex[:8]}",
            phone="+15555550101",
            channel_identifier="+15555550101",
            preferred_channel="bluebubbles",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


async def _make_session_async(
    factory: async_sessionmaker, user: User, channel: str = "bluebubbles"
) -> ChatSession:
    """Async peer of ``_make_session``."""
    async with factory() as db:
        now = datetime.datetime.now(datetime.UTC)
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            channel=channel,
            created_at=now,
            last_message_at=now,
        )
        db.add(cs)
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
    return cs


async def _add_message_async(
    factory: async_sessionmaker,
    cs: ChatSession,
    direction: str,
    seq: int,
    body: str,
    *,
    minutes_ago: int = 0,
) -> Message:
    """Async peer of ``_add_message``."""
    async with factory() as db:
        ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
        msg = Message(
            session_id=cs.id,
            seq=seq,
            direction=direction,
            body=body,
            external_message_id=f"ext-{seq}",
            media_urls_json="[]",
            timestamp=ts,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        db.expunge(msg)
    return msg


async def _make_user(db: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"recovery-test-{uuid.uuid4().hex[:8]}",
        phone="+15555550101",
        channel_identifier="+15555550101",
        preferred_channel="bluebubbles",
        onboarding_complete=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_session(db: AsyncSession, user: User, channel: str = "bluebubbles") -> ChatSession:
    now = datetime.datetime.now(datetime.UTC)
    cs = ChatSession(
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        channel=channel,
        created_at=now,
        last_message_at=now,
    )
    db.add(cs)
    await db.commit()
    await db.refresh(cs)
    return cs


async def _add_message(
    db: AsyncSession,
    cs: ChatSession,
    direction: str,
    seq: int,
    body: str,
    *,
    minutes_ago: int = 0,
) -> Message:
    ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
    msg = Message(
        session_id=cs.id,
        seq=seq,
        direction=direction,
        body=body,
        external_message_id=f"ext-{seq}",
        media_urls_json="[]",
        timestamp=ts,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


async def test_inbound_with_no_outbound_is_orphan() -> None:
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hello?", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = await _find_orphaned_inbounds_async(db, cutoff, datetime.datetime.now(datetime.UTC))

    assert len(rows) == 1
    msg, found_cs = rows[0]
    assert msg.body == "hello?"
    assert found_cs.session_id == cs.session_id


async def test_inbound_followed_by_outbound_is_not_orphan() -> None:
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hi", minutes_ago=2)
        await _add_message(db, cs, MessageDirection.OUTBOUND, seq=2, body="hi back", minutes_ago=1)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = await _find_orphaned_inbounds_async(db, cutoff, datetime.datetime.now(datetime.UTC))

    assert rows == []


async def test_inbound_outside_lookback_is_ignored() -> None:
    """Messages older than the cutoff are excluded so we don't dispatch a
    stale reply for something the user has long since moved past."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(
            db, cs, MessageDirection.INBOUND, seq=1, body="long ago", minutes_ago=120
        )

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = await _find_orphaned_inbounds_async(db, cutoff, datetime.datetime.now(datetime.UTC))

    assert rows == []


async def test_orphan_detection_is_per_session() -> None:
    """A second session's outbound must not satisfy the first session's
    inbound. Without the per-session join the EXISTS would let one user's
    activity declare another user's inbound 'processed'."""
    async with db_session_async() as db:
        user_a = await _make_user(db)
        user_b = await _make_user(db)
        cs_a = await _make_session(db, user_a)
        cs_b = await _make_session(db, user_b)

        await _add_message(db, cs_a, MessageDirection.INBOUND, seq=1, body="A asks", minutes_ago=2)
        await _add_message(
            db, cs_b, MessageDirection.OUTBOUND, seq=1, body="B reply", minutes_ago=1
        )

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = await _find_orphaned_inbounds_async(db, cutoff, datetime.datetime.now(datetime.UTC))

    # User A's inbound is still orphaned. User B has no inbound so nothing
    # to recover for them.
    assert len(rows) == 1
    msg, _ = rows[0]
    assert msg.body == "A asks"


# ---------------------------------------------------------------------------
# Dispatch input reconstruction
# ---------------------------------------------------------------------------


async def test_build_dispatch_inputs_round_trips_message_fields() -> None:
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        msg = await _add_message(
            db, cs, MessageDirection.INBOUND, seq=7, body="rebuild me", minutes_ago=5
        )

        result = await _build_dispatch_inputs_async(db, msg, cs)

    assert result is not None
    rebuilt_user, state, stored = result
    assert rebuilt_user.id == user.id
    assert state.session_id == cs.session_id
    assert state.user_id == user.id
    assert stored.seq == 7
    assert stored.body == "rebuild me"
    assert stored.direction == MessageDirection.INBOUND


async def test_build_dispatch_inputs_returns_none_when_user_missing() -> None:
    """Defense in depth: if the user row was deleted in the window between
    persist and recovery, log and skip rather than crash."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        msg = await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="x", minutes_ago=1)

        # Hard-delete user row so the rebuild lookup misses.
        await db.delete(user)
        await db.commit()

        result = await _build_dispatch_inputs_async(db, msg, cs)

    assert result is None


# ---------------------------------------------------------------------------
# Media refs reconstruction
# ---------------------------------------------------------------------------


def test_parse_media_refs_handles_empty_string() -> None:
    assert _parse_media_refs("") == []


def test_parse_media_refs_returns_pairs_with_empty_mime() -> None:
    """Mime types are not persisted; recovery returns empty mime so the
    media pipeline re-derives from the stored file."""
    assert _parse_media_refs('["abc", "def"]') == [("abc", ""), ("def", "")]


def test_parse_media_refs_handles_invalid_json() -> None:
    assert _parse_media_refs("not-json") == []
    assert _parse_media_refs("{}") == []


# ---------------------------------------------------------------------------
# End-to-end recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_recover_dispatches_each_orphan(async_db: async_sessionmaker) -> None:
    """The recovery loop calls ``_dispatch_to_pipeline`` once per orphan
    and returns the count of successes.

    Setup runs through the async fixture because
    ``recover_orphan_inbound_messages`` now reads via an
    ``AsyncSession``: a sync write committed on the per-test sync
    transaction lives on a different connection and would not be
    visible under READ COMMITTED. See AGENTS.md "Cross-API caveat".
    """
    user = await _make_user_async(async_db)
    cs = await _make_session_async(async_db, user)
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2
    )
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1
    )
    expected_user_id = user.id

    captured_user_ids: list[str] = []
    captured_bodies: list[str] = []

    async def capture_dispatch(**kwargs: object) -> None:
        # Read the user id while the recovery's session is still open
        # in this same task (kwargs["user"] is detached but its id was
        # loaded before expunge, so this access is safe).
        captured_user_ids.append(kwargs["user"].id)  # type: ignore[attr-defined]
        captured_bodies.append(kwargs["message"].body)  # type: ignore[attr-defined]

    with patch(
        "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
        new=capture_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 2
    assert captured_user_ids == [expected_user_id, expected_user_id]
    assert set(captured_bodies) == {"one", "two"}


@pytest.mark.asyncio()
async def test_recover_short_circuits_when_lookback_zero() -> None:
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(
            db, cs, MessageDirection.INBOUND, seq=1, body="should not run", minutes_ago=1
        )

    with (
        patch.object(settings, "inbound_recovery_lookback_minutes", 0),
        patch(
            "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 0
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio()
async def test_recover_continues_past_individual_dispatch_failure(
    async_db: async_sessionmaker,
) -> None:
    """One failing dispatch must not abort the whole sweep; the other
    orphan should still be recovered. Caller wraps the whole thing in a
    try/except too, but per-row resilience is what lets us deploy this
    in front of an in-flight queue without an all-or-nothing failure."""
    user = await _make_user_async(async_db)
    cs = await _make_session_async(async_db, user)
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2
    )
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1
    )

    call_count = {"n": 0}

    async def flaky_dispatch(**kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated dispatch failure")

    with patch(
        "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
        new=flaky_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    # Two attempts, one succeeded.
    assert call_count["n"] == 2
    assert recovered == 1


# ---------------------------------------------------------------------------
# Freshness floor (race with concurrent normal ingestion)
# ---------------------------------------------------------------------------


async def test_freshness_floor_excludes_brand_new_inbounds() -> None:
    """A message that landed milliseconds ago is the normal ingestion
    path's responsibility, not recovery. Without this floor a worker
    could race the in-flight MessageBatcher and double-dispatch a message
    that was about to be processed normally."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="just now", minutes_ago=0)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        # Floor at -30s: anything newer is excluded as too-fresh.
        floor = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30)
        rows = await _find_orphaned_inbounds_async(db, cutoff, floor)

    assert rows == []


async def test_freshness_floor_includes_messages_older_than_floor() -> None:
    """Sanity: floor at +0s (now) includes messages from the past."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="2m ago", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        floor = datetime.datetime.now(datetime.UTC)
        rows = await _find_orphaned_inbounds_async(db, cutoff, floor)

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Idempotency (a successful dispatch should produce an outbound that
# masks the orphan on a subsequent sweep)
# ---------------------------------------------------------------------------


async def test_orphan_is_no_longer_detected_after_outbound_lands() -> None:
    """The whole design rests on this invariant: once the agent loop runs
    and persists an outbound, the next sweep doesn't see the inbound as
    an orphan anymore. Simulate that by adding the outbound by hand and
    re-running the query."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hi", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        floor = datetime.datetime.now(datetime.UTC)
        before = await _find_orphaned_inbounds_async(db, cutoff, floor)
        assert len(before) == 1

        # Simulate the agent loop running and persisting its reply.
        await _add_message(db, cs, MessageDirection.OUTBOUND, seq=2, body="ok", minutes_ago=1)

        after = await _find_orphaned_inbounds_async(db, cutoff, floor)

    assert after == []


# ---------------------------------------------------------------------------
# Multi-worker advisory lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_recovery_skips_when_another_worker_holds_the_lock() -> None:
    """``pg_try_advisory_lock`` returns False to a second caller when
    another connection already holds the lock. The sweep must short-circuit
    in that case, otherwise N workers in a rolling restart would each
    re-dispatch the same orphans N times."""
    async with db_session_async() as db:
        user = await _make_user(db)
        cs = await _make_session(db, user)
        await _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2)

    with (
        patch(
            "backend.app.agent.inbound_recovery._try_acquire_lock_async",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 0
    mock_dispatch.assert_not_awaited()


# Note: the legacy sync ``TestInboundRecoveryLockSerialization`` class
# (advisory-lock thread-based regression matrix using the sync
# ``_pg_engine`` fixture) has been retired. The async port lives in
# ``tests/test_inbound_recovery_async.py::TestInboundRecoveryLockSerializationAsync``
# and exercises the same coupling invariants on ``AsyncConnection``.
