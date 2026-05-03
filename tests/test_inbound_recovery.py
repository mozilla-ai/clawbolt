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
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.inbound_recovery import (
    _build_dispatch_inputs,
    _find_orphaned_inbounds,
    _parse_media_refs,
    recover_orphan_inbound_messages,
)
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User


def _make_user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"recovery-test-{uuid.uuid4().hex[:8]}",
        phone="+15555550101",
        channel_identifier="+15555550101",
        preferred_channel="bluebubbles",
        onboarding_complete=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_session(db: Session, user: User, channel: str = "bluebubbles") -> ChatSession:
    now = datetime.datetime.now(datetime.UTC)
    cs = ChatSession(
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        is_active=True,
        channel=channel,
        last_compacted_seq=0,
        created_at=now,
        last_message_at=now,
    )
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return cs


def _add_message(
    db: Session,
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
    db.commit()
    db.refresh(msg)
    return msg


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def test_inbound_with_no_outbound_is_orphan(tmp_path: Path) -> None:
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hello?", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _find_orphaned_inbounds(db, cutoff)
    finally:
        db.close()

    assert len(rows) == 1
    msg, found_cs = rows[0]
    assert msg.body == "hello?"
    assert found_cs.session_id == cs.session_id


def test_inbound_followed_by_outbound_is_not_orphan(tmp_path: Path) -> None:
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hi", minutes_ago=2)
        _add_message(db, cs, MessageDirection.OUTBOUND, seq=2, body="hi back", minutes_ago=1)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _find_orphaned_inbounds(db, cutoff)
    finally:
        db.close()

    assert rows == []


def test_inbound_outside_lookback_is_ignored(tmp_path: Path) -> None:
    """Messages older than the cutoff are excluded so we don't dispatch a
    stale reply for something the user has long since moved past."""
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="long ago", minutes_ago=120)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _find_orphaned_inbounds(db, cutoff)
    finally:
        db.close()

    assert rows == []


def test_orphan_detection_is_per_session(tmp_path: Path) -> None:
    """A second session's outbound must not satisfy the first session's
    inbound. Without the per-session join the EXISTS would let one user's
    activity declare another user's inbound 'processed'."""
    db = SessionLocal()
    try:
        user_a = _make_user(db)
        user_b = _make_user(db)
        cs_a = _make_session(db, user_a)
        cs_b = _make_session(db, user_b)

        _add_message(db, cs_a, MessageDirection.INBOUND, seq=1, body="A asks", minutes_ago=2)
        _add_message(db, cs_b, MessageDirection.OUTBOUND, seq=1, body="B reply", minutes_ago=1)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _find_orphaned_inbounds(db, cutoff)
    finally:
        db.close()

    # User A's inbound is still orphaned. User B has no inbound so nothing
    # to recover for them.
    assert len(rows) == 1
    msg, _ = rows[0]
    assert msg.body == "A asks"


# ---------------------------------------------------------------------------
# Dispatch input reconstruction
# ---------------------------------------------------------------------------


def test_build_dispatch_inputs_round_trips_message_fields(tmp_path: Path) -> None:
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        msg = _add_message(
            db, cs, MessageDirection.INBOUND, seq=7, body="rebuild me", minutes_ago=5
        )

        result = _build_dispatch_inputs(db, msg, cs)
    finally:
        db.close()

    assert result is not None
    rebuilt_user, state, stored = result
    assert rebuilt_user.id == user.id
    assert state.session_id == cs.session_id
    assert state.user_id == user.id
    assert stored.seq == 7
    assert stored.body == "rebuild me"
    assert stored.direction == MessageDirection.INBOUND


def test_build_dispatch_inputs_returns_none_when_user_missing(tmp_path: Path) -> None:
    """Defense in depth: if the user row was deleted in the window between
    persist and recovery, log and skip rather than crash."""
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        msg = _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="x", minutes_ago=1)

        # Hard-delete user row so the rebuild lookup misses.
        db.delete(user)
        db.commit()

        result = _build_dispatch_inputs(db, msg, cs)
    finally:
        db.close()

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
async def test_recover_dispatches_each_orphan(tmp_path: Path) -> None:
    """The recovery loop calls ``_dispatch_to_pipeline`` once per orphan
    and returns the count of successes."""
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2)
        _add_message(db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1)
        expected_user_id = user.id
    finally:
        db.close()

    captured_user_ids: list[str] = []
    captured_bodies: list[str] = []

    async def capture_dispatch(**kwargs: object) -> None:
        # Read the user id while the recovery's session is still open
        # in this same task (kwargs["user"] is detached but its id was
        # loaded before expunge, so this access is safe).
        captured_user_ids.append(kwargs["user"].id)  # type: ignore[attr-defined]
        captured_bodies.append(kwargs["message"].body)  # type: ignore[attr-defined]

    with patch(
        "backend.app.agent.ingestion._dispatch_to_pipeline",
        new=capture_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 2
    assert captured_user_ids == [expected_user_id, expected_user_id]
    assert set(captured_bodies) == {"one", "two"}


@pytest.mark.asyncio()
async def test_recover_short_circuits_when_lookback_zero(tmp_path: Path) -> None:
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="should not run", minutes_ago=1)
    finally:
        db.close()

    with (
        patch.object(settings, "inbound_recovery_lookback_minutes", 0),
        patch(
            "backend.app.agent.ingestion._dispatch_to_pipeline",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 0
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio()
async def test_recover_continues_past_individual_dispatch_failure(
    tmp_path: Path,
) -> None:
    """One failing dispatch must not abort the whole sweep; the other
    orphan should still be recovered. Caller wraps the whole thing in a
    try/except too, but per-row resilience is what lets us deploy this
    in front of an in-flight queue without an all-or-nothing failure."""
    db = SessionLocal()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2)
        _add_message(db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1)
    finally:
        db.close()

    call_count = {"n": 0}

    async def flaky_dispatch(**kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated dispatch failure")

    with patch(
        "backend.app.agent.ingestion._dispatch_to_pipeline",
        new=flaky_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    # Two attempts, one succeeded.
    assert call_count["n"] == 2
    assert recovered == 1
