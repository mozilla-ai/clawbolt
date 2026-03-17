"""Integration test: full message round-trip through the bus consumer.

InboundMessage -> process_inbound_from_bus -> agent pipeline -> outbound
"""

from unittest.mock import AsyncMock, patch

import pytest

import backend.app.database as _db_module
from backend.app.agent.file_store import (
    StoredMessage,
    UserData,
)
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
from backend.app.agent.session_db import get_session_store
from backend.app.bus import message_bus
from backend.app.models import User
from tests.mocks.llm import make_text_response


async def _get_all_messages(user_id: str) -> list[StoredMessage]:
    """Helper to retrieve all stored messages for a user."""
    store = get_session_store(user_id)
    session, _is_new = await store.get_or_create_session()
    return list(session.messages)


@pytest.mark.asyncio
async def test_full_message_round_trip(
    test_user: UserData,
) -> None:
    """End-to-end: inbound message -> agent processes -> outbound reply stored."""
    inbound = InboundMessage(
        channel="telegram",
        sender_id=test_user.channel_identifier,
        text="I need a quote for a 12x12 composite deck",
    )

    with (
        patch(
            "backend.app.agent.core.amessages",
            new_callable=AsyncMock,
            return_value=make_text_response("I can help with that deck estimate!"),
        ),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
    ):
        await process_inbound_from_bus(inbound)

    # Verify inbound message stored
    messages = await _get_all_messages(test_user.id)
    inbound_msgs = [m for m in messages if m.direction == "inbound"]
    assert len(inbound_msgs) == 1
    assert inbound_msgs[0].body == "I need a quote for a 12x12 composite deck"

    # Verify processed_context was saved
    assert inbound_msgs[0].processed_context is not None

    # Verify outbound message stored
    outbound_msgs = [m for m in messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 1
    assert outbound_msgs[0].body == "I can help with that deck estimate!"

    # Verify outbound reply published to bus (for outbound dispatcher)
    assert not message_bus.outbound.empty()
    found_reply = False
    while not message_bus.outbound.empty():
        outbound = await message_bus.consume_outbound()
        if outbound.is_typing_indicator:
            continue
        assert outbound.channel == "telegram"
        assert outbound.content == "I can help with that deck estimate!"
        found_reply = True
        break
    assert found_reply


@pytest.mark.asyncio
async def test_full_message_round_trip_new_user() -> None:
    """New user sends message -> auto-created -> agent replies."""
    inbound = InboundMessage(
        channel="telegram",
        sender_id="777888999",
        text="Hi, I'm a plumber",
    )

    with (
        patch(
            "backend.app.agent.core.amessages",
            new_callable=AsyncMock,
            return_value=make_text_response("Welcome to Clawbolt! What's your name?"),
        ),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
    ):
        await process_inbound_from_bus(inbound)

    # User was auto-created
    from backend.app.models import ChannelRoute

    db = _db_module.SessionLocal()
    try:
        route = db.query(ChannelRoute).filter_by(channel_identifier="777888999").first()
        if route:
            user = db.query(User).filter_by(id=route.user_id).first()
            if user:
                db.expunge(user)
        else:
            user = db.query(User).filter_by(channel_identifier="777888999").first()
            if user:
                db.expunge(user)
    finally:
        db.close()
    assert user is not None

    # Messages stored
    messages = await _get_all_messages(user.id)
    assert len(messages) == 2  # inbound + outbound
    directions = {m.direction for m in messages}
    assert directions == {"inbound", "outbound"}


@pytest.mark.asyncio
async def test_full_message_agent_failure_still_stores_inbound(
    test_user: UserData,
) -> None:
    """If the agent pipeline fails, inbound is stored but fallback is not."""
    inbound = InboundMessage(
        channel="telegram",
        sender_id=test_user.channel_identifier,
        text="Hello",
    )

    with (
        patch(
            "backend.app.agent.core.amessages",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM service down"),
        ),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
    ):
        await process_inbound_from_bus(inbound)

    # Inbound message still stored
    messages = await _get_all_messages(test_user.id)
    inbound_msgs = [m for m in messages if m.direction == "inbound"]
    assert len(inbound_msgs) == 1

    # Fallback reply is NOT stored (avoids poisoning conversation context)
    outbound_msgs = [m for m in messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0
