"""Integration test: full SMS round-trip through the system.

Webhook → media pipeline → agent → SMS reply
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import Contractor, Message
from backend.app.services.twilio_service import TwilioService
from tests.mocks.llm import make_text_response
from tests.mocks.twilio import make_twilio_webhook_payload


def test_full_sms_round_trip(
    client: TestClient,
    db_session: Session,
    test_contractor: Contractor,
    mock_twilio_service: TwilioService,
) -> None:
    """End-to-end: inbound SMS → agent processes → outbound SMS reply."""
    with patch(
        "backend.app.agent.core.acompletion",
        new_callable=AsyncMock,
        return_value=make_text_response("I can help with that deck estimate!"),
    ):
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="I need a quote for a 12x12 composite deck",
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    assert response.status_code == 200
    assert "<Response/>" in response.text

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

    # Verify reply SMS was sent via TwilioService
    mock_twilio_service.send_sms.assert_called_once_with(  # type: ignore[union-attr]
        to=test_contractor.phone,
        body="I can help with that deck estimate!",
    )


def test_full_sms_round_trip_new_contractor(
    client: TestClient,
    db_session: Session,
    mock_twilio_service: TwilioService,
) -> None:
    """New contractor sends SMS → auto-created → agent replies."""
    with patch(
        "backend.app.agent.core.acompletion",
        new_callable=AsyncMock,
        return_value=make_text_response("Welcome to Backshop! What's your name?"),
    ):
        payload = make_twilio_webhook_payload(
            from_number="+15559998888",
            body="Hi, I'm a plumber",
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    assert response.status_code == 200

    # Contractor was auto-created
    from backend.app.models import Contractor as C

    contractor = db_session.query(C).filter(C.phone == "+15559998888").first()
    assert contractor is not None

    # Messages stored
    messages = db_session.query(Message).all()
    assert len(messages) == 2  # inbound + outbound
    directions = {m.direction for m in messages}
    assert directions == {"inbound", "outbound"}

    # Reply sent
    mock_twilio_service.send_sms.assert_called_once()  # type: ignore[union-attr]


def test_full_sms_agent_failure_still_returns_200(
    client: TestClient,
    db_session: Session,
    test_contractor: Contractor,
    mock_twilio_service: TwilioService,
) -> None:
    """If the entire agent pipeline fails, webhook still returns 200."""
    with patch(
        "backend.app.agent.core.acompletion",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM service down"),
    ):
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="Hello",
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    # Webhook returns 200 even on agent failure (Twilio needs 200)
    assert response.status_code == 200

    # Inbound message still stored
    inbound = db_session.query(Message).filter(Message.direction == "inbound").first()
    assert inbound is not None

    # Fallback reply sent
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert "trouble" in outbound.body.lower()
