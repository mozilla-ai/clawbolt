"""Tests for the startup retry of stale pending compaction events.

``trigger_compaction_for_dropped`` advances the trim watermark before the
async compaction LLM call runs; a crash mid-call leaves the
``CompactionEvent`` row ``'pending'`` forever and the seq range's facts
were never extracted (issue #1431). ``recover_pending_compactions``
sweeps those rows on startup and re-runs ``compact_session`` against the
recorded seq range, bounded by ``retry_count``.
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.app.agent.compaction_recovery import (
    _MAX_ATTEMPTS,
    recover_pending_compactions,
)
from backend.app.agent.file_store import UserData
from backend.app.agent.memory_db import get_memory_store
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, CompactionEvent, Message, User
from tests.mocks.llm import make_text_response


async def _seed_session_with_messages(user: User | UserData, message_count: int) -> ChatSession:
    """Insert a ChatSession with alternating inbound/outbound messages."""
    async with db_session_async() as db:
        cs = ChatSession(
            session_id=f"session-{user.id}",
            user_id=user.id,
            channel="webchat",
            initial_system_prompt="",
        )
        db.add(cs)
        await db.flush()
        for i in range(1, message_count + 1):
            db.add(
                Message(
                    session_id=cs.id,
                    seq=i,
                    direction=(
                        MessageDirection.INBOUND if i % 2 == 1 else MessageDirection.OUTBOUND
                    ),
                    body=f"msg {i}",
                    processed_context="",
                    tool_interactions_json="",
                    external_message_id="",
                    media_urls_json="[]",
                )
            )
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
        return cs


async def _insert_pending_event(
    user_id: str,
    min_seq: int,
    max_seq: int,
    age_minutes: int = 60,
    retry_count: int = 0,
) -> int:
    """Insert a 'pending' CompactionEvent aged *age_minutes* into the past."""
    triggered_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=age_minutes)
    async with db_session_async() as db:
        event = CompactionEvent(
            user_id=user_id,
            triggered_at=triggered_at,
            status="pending",
            min_message_seq=min_seq,
            max_message_seq=max_seq,
            trimmed_count=max_seq - min_seq + 1,
            retry_count=retry_count,
        )
        db.add(event)
        await db.commit()
        assert event.id is not None
        return event.id


async def _read_event(event_id: int) -> CompactionEvent:
    async with db_session_async() as db:
        event = (await db.execute(select(CompactionEvent).filter_by(id=event_id))).scalar_one()
        db.expunge(event)
        return event


@pytest.mark.asyncio()
async def test_recovers_stale_pending_event(test_user: UserData) -> None:
    """A stale pending event is retried, completed, and its facts land."""
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(test_user.id, min_seq=1, max_seq=4)

    mock_response = make_text_response(
        json.dumps({"memory_update": "## Facts\n- fact: recovered", "summary": ""})
    )
    with patch("backend.app.agent.compaction.amessages", return_value=mock_response):
        completed = await recover_pending_compactions()

    assert completed == 1
    event = await _read_event(event_id)
    assert event.status == "completed"
    assert event.retry_count == 1

    content = await get_memory_store(test_user.id).read_memory_async()
    assert "fact: recovered" in content


@pytest.mark.asyncio()
async def test_skips_fresh_pending_event(test_user: UserData) -> None:
    """Events younger than the grace floor are in-flight, not stale."""
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(test_user.id, min_seq=1, max_seq=4, age_minutes=0)

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        completed = await recover_pending_compactions()

    assert completed == 0
    mock_llm.assert_not_called()
    event = await _read_event(event_id)
    assert event.status == "pending"
    assert event.retry_count == 0


@pytest.mark.asyncio()
async def test_skips_event_beyond_lookback(test_user: UserData) -> None:
    """Events older than the lookback window are not retried."""
    await _seed_session_with_messages(test_user, message_count=6)
    lookback = settings.compaction_retry_lookback_minutes
    event_id = await _insert_pending_event(
        test_user.id, min_seq=1, max_seq=4, age_minutes=lookback + 60
    )

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        completed = await recover_pending_compactions()

    assert completed == 0
    mock_llm.assert_not_called()
    assert (await _read_event(event_id)).retry_count == 0


@pytest.mark.asyncio()
async def test_skips_exhausted_event(test_user: UserData) -> None:
    """Rows at the attempt cap stop being selected (no infinite retry)."""
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(
        test_user.id, min_seq=1, max_seq=4, retry_count=_MAX_ATTEMPTS
    )

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        completed = await recover_pending_compactions()

    assert completed == 0
    mock_llm.assert_not_called()
    event = await _read_event(event_id)
    assert event.status == "pending"
    assert event.retry_count == _MAX_ATTEMPTS


@pytest.mark.asyncio()
async def test_failed_retry_keeps_pending_and_counts_attempt(
    test_user: UserData,
) -> None:
    """An LLM failure leaves the row pending; the attempt is consumed."""
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(test_user.id, min_seq=1, max_seq=4)

    with patch("backend.app.agent.compaction.amessages", side_effect=RuntimeError("provider down")):
        completed = await recover_pending_compactions()

    assert completed == 0
    event = await _read_event(event_id)
    assert event.status == "pending"
    assert event.retry_count == 1


@pytest.mark.asyncio()
async def test_empty_range_exhausts_event(test_user: UserData) -> None:
    """A range with no recoverable messages stops being selected."""
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(test_user.id, min_seq=50, max_seq=60)

    with patch("backend.app.agent.compaction.amessages") as mock_llm:
        completed = await recover_pending_compactions()

    assert completed == 0
    mock_llm.assert_not_called()
    event = await _read_event(event_id)
    assert event.status == "pending"
    assert event.retry_count == _MAX_ATTEMPTS


@pytest.mark.asyncio()
async def test_sweep_disabled_by_zero_lookback(test_user: UserData) -> None:
    await _seed_session_with_messages(test_user, message_count=6)
    event_id = await _insert_pending_event(test_user.id, min_seq=1, max_seq=4)

    with (
        patch.object(settings, "compaction_retry_lookback_minutes", 0),
        patch("backend.app.agent.compaction.amessages") as mock_llm,
    ):
        completed = await recover_pending_compactions()

    assert completed == 0
    mock_llm.assert_not_called()
    assert (await _read_event(event_id)).retry_count == 0
