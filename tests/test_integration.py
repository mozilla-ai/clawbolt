"""Integration test: full message round-trip through the bus consumer.

InboundMessage -> process_inbound_from_bus -> agent pipeline -> outbound
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.file_store import (
    ContractorData,
    StoredMessage,
    get_contractor_store,
    get_session_store,
)
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
from backend.app.bus import message_bus
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response


async def _get_all_messages(contractor_id: int) -> list[StoredMessage]:
    """Helper to retrieve all stored messages for a contractor."""
    store = get_session_store(contractor_id)
    session, _is_new = await store.get_or_create_session()
    return list(session.messages)


@pytest.mark.asyncio
async def test_full_message_round_trip(
    test_contractor: ContractorData,
    mock_messaging_service: MessagingService,
) -> None:
    """End-to-end: inbound message -> agent processes -> outbound reply stored."""
    inbound = InboundMessage(
        channel="telegram",
        sender_id=test_contractor.channel_identifier,
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
        await process_inbound_from_bus(inbound, mock_messaging_service)

    # Verify inbound message stored
    messages = await _get_all_messages(test_contractor.id)
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
    outbound = await message_bus.consume_outbound()
    assert outbound.channel == "telegram"
    assert outbound.content == "I can help with that deck estimate!"


@pytest.mark.asyncio
async def test_full_message_round_trip_new_contractor(
    mock_messaging_service: MessagingService,
) -> None:
    """New contractor sends message -> auto-created -> agent replies."""
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
        await process_inbound_from_bus(inbound, mock_messaging_service)

    # Contractor was auto-created
    store = get_contractor_store()
    contractor = await store.get_by_channel("777888999")
    assert contractor is not None

    # Messages stored
    messages = await _get_all_messages(contractor.id)
    assert len(messages) == 2  # inbound + outbound
    directions = {m.direction for m in messages}
    assert directions == {"inbound", "outbound"}


@pytest.mark.asyncio
async def test_full_message_agent_failure_still_stores_inbound(
    test_contractor: ContractorData,
    mock_messaging_service: MessagingService,
) -> None:
    """If the agent pipeline fails, inbound is stored but fallback is not."""
    inbound = InboundMessage(
        channel="telegram",
        sender_id=test_contractor.channel_identifier,
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
        await process_inbound_from_bus(inbound, mock_messaging_service)

    # Inbound message still stored
    messages = await _get_all_messages(test_contractor.id)
    inbound_msgs = [m for m in messages if m.direction == "inbound"]
    assert len(inbound_msgs) == 1

    # Fallback reply is NOT stored (avoids poisoning conversation context)
    outbound_msgs = [m for m in messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0
