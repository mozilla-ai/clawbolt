from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import Contractor, Conversation, Message
from tests.mocks.twilio import make_twilio_webhook_payload


def test_inbound_webhook_returns_200(client: TestClient) -> None:
    """Valid webhook payload should return 200 with empty TwiML."""
    payload = make_twilio_webhook_payload()
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200
    assert "<Response/>" in response.text


def test_inbound_webhook_stores_message(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Inbound message should be stored in the database."""
    payload = make_twilio_webhook_payload(
        from_number=test_contractor.phone,
        body="I need a quote for kitchen remodel",
    )
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].direction == "inbound"
    assert messages[0].body == "I need a quote for kitchen remodel"


def test_inbound_webhook_extracts_media_urls(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Media URLs should be extracted and stored."""
    payload = make_twilio_webhook_payload(
        from_number=test_contractor.phone,
        body="Here are the photos",
        num_media=2,
        media_urls=["https://api.twilio.com/media1.jpg", "https://api.twilio.com/media2.jpg"],
        media_types=["image/jpeg", "image/jpeg"],
    )
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "media1.jpg" in messages[0].media_urls_json
    assert "media2.jpg" in messages[0].media_urls_json


def test_inbound_webhook_creates_contractor_if_new(client: TestClient, db_session: Session) -> None:
    """Unknown phone number should create a new contractor."""
    payload = make_twilio_webhook_payload(from_number="+15559999999", body="Hi")
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    contractor = db_session.query(Contractor).filter(Contractor.phone == "+15559999999").first()
    assert contractor is not None


def test_inbound_webhook_creates_conversation(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Should create a conversation for the contractor."""
    payload = make_twilio_webhook_payload(from_number=test_contractor.phone, body="Hello")
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    conversations = (
        db_session.query(Conversation)
        .filter(Conversation.contractor_id == test_contractor.id)
        .all()
    )
    assert len(conversations) == 1
    assert conversations[0].is_active is True
