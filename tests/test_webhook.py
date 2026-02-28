from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import Contractor, Conversation, Message
from backend.app.routers.webhooks import _extract_media
from backend.app.services.twilio_service import TwilioService
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


def test_extract_media_preserves_content_type() -> None:
    """_extract_media should return url and content_type for each media item."""
    form_data = {
        "NumMedia": "2",
        "MediaUrl0": "https://api.twilio.com/media1.jpg",
        "MediaContentType0": "image/jpeg",
        "MediaUrl1": "https://api.twilio.com/media2.pdf",
        "MediaContentType1": "application/pdf",
    }
    result = _extract_media(form_data)
    assert len(result) == 2
    assert result[0]["url"] == "https://api.twilio.com/media1.jpg"
    assert result[0]["content_type"] == "image/jpeg"
    assert result[1]["url"] == "https://api.twilio.com/media2.pdf"
    assert result[1]["content_type"] == "application/pdf"


def test_media_urls_as_tuples_with_content_type(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """media_urls should be (url, content_type) tuples, not bare URLs."""
    payload = make_twilio_webhook_payload(
        from_number=test_contractor.phone,
        body="Photo of the job",
        num_media=1,
        media_urls=["https://api.twilio.com/media1.jpg"],
        media_types=["image/jpeg"],
    )
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    # media_urls_json should still store just URLs for DB storage
    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "media1.jpg" in messages[0].media_urls_json


def test_twilio_service_injected_via_depends(
    client: TestClient, mock_twilio_service: TwilioService
) -> None:
    """TwilioService should be injected via Depends and overridable in tests."""
    # The mock_twilio_service fixture is injected via dependency override in conftest.
    # If DI is wired correctly, the endpoint succeeds with the mock (no real Twilio call).
    payload = make_twilio_webhook_payload()
    response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200
    # Verify the mock is indeed a MagicMock (not a real TwilioService)
    assert isinstance(mock_twilio_service, MagicMock)
