import datetime

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.context import (
    CONVERSATION_TIMEOUT_HOURS,
    get_or_create_conversation,
    load_conversation_history,
)
from backend.app.models import Contractor, Conversation, Message


@pytest.fixture()
def conversation(db_session: Session, test_contractor: Contractor) -> Conversation:
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    return conv


@pytest.mark.asyncio()
async def test_load_history_chronological_order(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """History should be in chronological order."""
    for i in range(3):
        msg = Message(
            conversation_id=conversation.id,
            direction="inbound" if i % 2 == 0 else "outbound",
            body=f"Message {i}",
        )
        db_session.add(msg)
    db_session.commit()

    # Add the "current" message
    current = Message(conversation_id=conversation.id, direction="inbound", body="Current")
    db_session.add(current)
    db_session.commit()

    history = await load_conversation_history(db_session, conversation.id)
    # Should exclude the current (most recent) message
    assert len(history) == 3
    assert history[0]["content"] == "Message 0"
    assert history[1]["content"] == "Message 1"
    assert history[2]["content"] == "Message 2"


@pytest.mark.asyncio()
async def test_load_history_roles(
    db_session: Session,
    conversation: Conversation,
) -> None:
    """Inbound = user, outbound = assistant."""
    db_session.add(Message(conversation_id=conversation.id, direction="inbound", body="Hi"))
    db_session.add(Message(conversation_id=conversation.id, direction="outbound", body="Hello!"))
    db_session.add(Message(conversation_id=conversation.id, direction="inbound", body="Current"))
    db_session.commit()

    history = await load_conversation_history(db_session, conversation.id)
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"


@pytest.mark.asyncio()
async def test_load_history_limit(
    db_session: Session,
    conversation: Conversation,
) -> None:
    """History should be limited to N messages."""
    for i in range(10):
        db_session.add(
            Message(conversation_id=conversation.id, direction="inbound", body=f"Msg {i}")
        )
    db_session.commit()

    history = await load_conversation_history(db_session, conversation.id, limit=5)
    # 5 loaded, minus 1 for current = 4
    assert len(history) == 4


@pytest.mark.asyncio()
async def test_load_history_prefers_processed_context(
    db_session: Session,
    conversation: Conversation,
) -> None:
    """History should use processed_context over raw body when available."""
    msg = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body="Check this photo",
        processed_context="[Text message]: 'Check this photo'\n[Photo 1]: A damaged deck railing",
    )
    db_session.add(msg)
    # Add current message
    db_session.add(Message(conversation_id=conversation.id, direction="inbound", body="Current"))
    db_session.commit()

    history = await load_conversation_history(db_session, conversation.id)
    assert "damaged deck railing" in history[0]["content"]


@pytest.mark.asyncio()
async def test_load_history_empty_conversation(
    db_session: Session,
    conversation: Conversation,
) -> None:
    """Empty conversation should return empty history."""
    history = await load_conversation_history(db_session, conversation.id)
    assert history == []


@pytest.mark.asyncio()
async def test_load_history_single_message(
    db_session: Session,
    conversation: Conversation,
) -> None:
    """Single message (current) should return empty history."""
    db_session.add(Message(conversation_id=conversation.id, direction="inbound", body="Only msg"))
    db_session.commit()

    history = await load_conversation_history(db_session, conversation.id)
    assert history == []


@pytest.mark.asyncio()
async def test_get_or_create_conversation_new(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Should create a new conversation when none exists."""
    conv, is_new = await get_or_create_conversation(db_session, test_contractor.id)
    assert is_new is True
    assert conv.contractor_id == test_contractor.id
    assert conv.is_active is True


@pytest.mark.asyncio()
async def test_get_or_create_conversation_existing_active(
    db_session: Session,
    test_contractor: Contractor,
    conversation: Conversation,
) -> None:
    """Should return existing active conversation within timeout."""
    # Update last_message_at to be recent
    conversation.last_message_at = datetime.datetime.now(datetime.UTC)
    db_session.commit()

    conv, is_new = await get_or_create_conversation(db_session, test_contractor.id)
    assert is_new is False
    assert conv.id == conversation.id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_expired(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Should create new conversation when existing one has timed out."""
    # Create an old conversation
    old_conv = Conversation(
        contractor_id=test_contractor.id,
        is_active=True,
    )
    db_session.add(old_conv)
    db_session.commit()

    # Manually set last_message_at to past the timeout
    old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=CONVERSATION_TIMEOUT_HOURS + 1
    )
    old_conv.last_message_at = old_time
    db_session.commit()

    conv, is_new = await get_or_create_conversation(db_session, test_contractor.id)
    assert is_new is True
    assert conv.id != old_conv.id


@pytest.mark.asyncio()
async def test_get_or_create_conversation_with_external_session_id(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """New conversation should store external session ID."""
    conv, is_new = await get_or_create_conversation(
        db_session, test_contractor.id, external_session_id="session_abc123"
    )
    assert is_new is True
    assert conv.external_session_id == "session_abc123"


@pytest.mark.asyncio()
async def test_get_or_create_conversation_custom_timeout(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Custom timeout should be respected."""
    # Create a conversation 2 hours old
    conv1 = Conversation(contractor_id=test_contractor.id, is_active=True)
    db_session.add(conv1)
    db_session.commit()
    conv1.last_message_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
    db_session.commit()

    # With 1-hour timeout, should create new
    conv, is_new = await get_or_create_conversation(db_session, test_contractor.id, timeout_hours=1)
    assert is_new is True

    # Clean up the new conversation for next assertion
    db_session.delete(conv)
    db_session.commit()

    # With 3-hour timeout, should reuse
    conv, is_new = await get_or_create_conversation(db_session, test_contractor.id, timeout_hours=3)
    assert is_new is False
