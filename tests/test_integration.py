"""Integration test: full message round-trip through the system.

Webhook -> media pipeline -> agent -> reply
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import Contractor, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response
from tests.mocks.telegram import make_telegram_update_payload


def test_full_message_round_trip(
    client: TestClient,
    db_session: Session,
    test_contractor: Contractor,
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
    inbound = db_session.query(Message).filter(Message.direction == "inbound").first()
    assert inbound is not None
    assert inbound.body == "I need a quote for a 12x12 composite deck"

    # Verify processed_context was saved
    assert inbound.processed_context is not None

    # Verify outbound message stored
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "I can help with that deck estimate!"

    # Verify reply was sent via MessagingService
    mock_messaging_service.send_text.assert_called_once_with(  # type: ignore[union-attr]
        to=test_contractor.channel_identifier,
        body="I can help with that deck estimate!",
    )


def test_full_message_round_trip_new_contractor(
    client: TestClient,
    db_session: Session,
    mock_messaging_service: MessagingService,
) -> None:
    """New contractor sends message -> auto-created -> agent replies."""
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
    contractor = (
        db_session.query(Contractor).filter(Contractor.channel_identifier == "777888999").first()
    )
    assert contractor is not None

    # Messages stored
    messages = db_session.query(Message).all()
    assert len(messages) == 2  # inbound + outbound
    directions = {m.direction for m in messages}
    assert directions == {"inbound", "outbound"}

    # Reply sent
    mock_messaging_service.send_text.assert_called_once()  # type: ignore[union-attr]


def test_full_message_agent_failure_still_returns_200(
    client: TestClient,
    db_session: Session,
    test_contractor: Contractor,
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
    inbound = db_session.query(Message).filter(Message.direction == "inbound").first()
    assert inbound is not None

    # Fallback reply is NOT stored (avoids poisoning conversation context)
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is None
