"""Tests for the trim-to-compaction continuity note (issue #1432).

The trim watermark advances synchronously, but the compaction LLM call
that extracts the dropped rows' facts is async. While a recent
``'pending'`` compaction event exists, ``build_pending_compaction_note``
rebuilds a deterministic summary of the covered rows so the agent's
context contains either the compacted facts or the note, never neither.
"""

from __future__ import annotations

import datetime

import pytest

from backend.app.agent.compaction_note import build_pending_compaction_note
from backend.app.agent.file_store import UserData
from backend.app.database import db_session_async
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, CompactionEvent, Message


async def _seed_session_with_messages(user_id: str, message_count: int) -> ChatSession:
    """Insert a ChatSession with alternating inbound/outbound messages."""
    async with db_session_async() as db:
        cs = ChatSession(
            session_id=f"session-{user_id}",
            user_id=user_id,
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
                    body=f"deck quote topic {i}",
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


async def _insert_event(
    user_id: str,
    min_seq: int,
    max_seq: int,
    status: str = "pending",
    age_minutes: int = 1,
) -> int:
    triggered_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=age_minutes)
    async with db_session_async() as db:
        event = CompactionEvent(
            user_id=user_id,
            triggered_at=triggered_at,
            status=status,
            min_message_seq=min_seq,
            max_message_seq=max_seq,
            trimmed_count=max_seq - min_seq + 1,
        )
        db.add(event)
        await db.commit()
        assert event.id is not None
        return event.id


@pytest.mark.asyncio()
async def test_no_note_without_events(test_user: UserData) -> None:
    await _seed_session_with_messages(test_user.id, message_count=6)
    assert await build_pending_compaction_note(test_user.id) == ""


@pytest.mark.asyncio()
async def test_note_while_event_pending(test_user: UserData) -> None:
    """A recent pending event produces a summary of the covered rows."""
    await _seed_session_with_messages(test_user.id, message_count=6)
    await _insert_event(test_user.id, min_seq=1, max_seq=4)

    note = await build_pending_compaction_note(test_user.id)
    assert note != ""
    # The summarizer surfaces user topics from the covered range.
    assert "deck quote topic 1" in note
    # And the framing tells the agent the facts are en route to memory.
    assert "being written to your memory" in note


@pytest.mark.asyncio()
async def test_note_is_deterministic_across_turns(test_user: UserData) -> None:
    """The note is byte-identical while the same event stays pending."""
    await _seed_session_with_messages(test_user.id, message_count=6)
    await _insert_event(test_user.id, min_seq=1, max_seq=4)

    first = await build_pending_compaction_note(test_user.id)
    second = await build_pending_compaction_note(test_user.id)
    assert first == second != ""


@pytest.mark.asyncio()
async def test_no_note_after_completion(test_user: UserData) -> None:
    """Completed events stop producing the note: MEMORY.md has the facts."""
    await _seed_session_with_messages(test_user.id, message_count=6)
    await _insert_event(test_user.id, min_seq=1, max_seq=4, status="completed")

    assert await build_pending_compaction_note(test_user.id) == ""


@pytest.mark.asyncio()
async def test_no_note_for_stale_pending_event(test_user: UserData) -> None:
    """A long-stuck pending event must not pin a stale note forever."""
    await _seed_session_with_messages(test_user.id, message_count=6)
    await _insert_event(test_user.id, min_seq=1, max_seq=4, age_minutes=120)

    assert await build_pending_compaction_note(test_user.id) == ""


@pytest.mark.asyncio()
async def test_note_covers_multiple_pending_events(test_user: UserData) -> None:
    await _seed_session_with_messages(test_user.id, message_count=10)
    await _insert_event(test_user.id, min_seq=1, max_seq=3)
    await _insert_event(test_user.id, min_seq=4, max_seq=6)

    note = await build_pending_compaction_note(test_user.id)
    assert "deck quote topic 1" in note
    assert "deck quote topic 5" in note
