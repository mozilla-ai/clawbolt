"""Integration test: full message round-trip through the system.

Webhook -> media pipeline -> agent -> reply
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.agent.file_store import (
    ContractorData,
    StoredMessage,
    get_contractor_store,
    get_session_store,
)
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response
from tests.mocks.telegram import make_telegram_update_payload


def _get_all_messages(contractor_id: int) -> list[StoredMessage]:
    """Helper to retrieve all stored messages for a contractor from the file session store."""
    import asyncio

    store = get_session_store(contractor_id)

    async def _fetch() -> list[StoredMessage]:
        session, _is_new = await store.get_or_create_session()
        return list(session.messages)

    return asyncio.get_event_loop().run_until_complete(_fetch())


def test_full_message_round_trip(
    client: TestClient,
    test_contractor: ContractorData,
    mock_messaging_service: MessagingService,
) -> None:
    """End-to-end: inbound message -> agent processes -> outbound reply."""
    with patch(
        "backend.app.agent.core.amessages",
        new_callable=AsyncMock,
        return_value=make_text_response("I can help with that deck estimate!"),
    ):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="I need a quote for a 12x12 composite deck",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify inbound message stored
    messages = _get_all_messages(test_contractor.id)
    inbound = [m for m in messages if m.direction == "inbound"]
    assert len(inbound) == 1
    assert inbound[0].body == "I need a quote for a 12x12 composite deck"

    # Verify processed_context was saved
    assert inbound[0].processed_context is not None

    # Verify outbound message stored
    outbound = [m for m in messages if m.direction == "outbound"]
    assert len(outbound) == 1
    assert outbound[0].body == "I can help with that deck estimate!"

    # Verify reply was sent via MessagingService
    mock_messaging_service.send_text.assert_called_once_with(  # type: ignore[union-attr]
        to=test_contractor.channel_identifier,
        body="I can help with that deck estimate!",
    )


def test_full_message_round_trip_new_contractor(
    client: TestClient,
    mock_messaging_service: MessagingService,
) -> None:
    """New contractor sends message -> auto-created -> agent replies."""
    import asyncio

    with patch(
        "backend.app.agent.core.amessages",
        new_callable=AsyncMock,
        return_value=make_text_response("Welcome to Clawbolt! What's your name?"),
    ):
        payload = make_telegram_update_payload(
            chat_id=777888999,
            text="Hi, I'm a plumber",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200

    # Contractor was auto-created
    store = get_contractor_store()
    contractor = asyncio.get_event_loop().run_until_complete(store.get_by_channel("777888999"))
    assert contractor is not None

    # Messages stored
    messages = _get_all_messages(contractor.id)
    assert len(messages) == 2  # inbound + outbound
    directions = {m.direction for m in messages}
    assert directions == {"inbound", "outbound"}

    # Reply sent
    mock_messaging_service.send_text.assert_called_once()  # type: ignore[union-attr]


def test_full_message_agent_failure_still_returns_200(
    client: TestClient,
    test_contractor: ContractorData,
    mock_messaging_service: MessagingService,
) -> None:
    """If the entire agent pipeline fails, webhook still returns 200."""
    with patch(
        "backend.app.agent.core.amessages",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM service down"),
    ):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="Hello",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    # Webhook returns 200 even on agent failure
    assert response.status_code == 200

    # Inbound message still stored
    messages = _get_all_messages(test_contractor.id)
    inbound = [m for m in messages if m.direction == "inbound"]
    assert len(inbound) == 1

    # Fallback reply is NOT stored (avoids poisoning conversation context)
    outbound = [m for m in messages if m.direction == "outbound"]
    assert len(outbound) == 0
