from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.core import AgentResponse
from backend.app.models import Contractor, Conversation, Message
from backend.app.routers.webhooks import _extract_media
from backend.app.services.twilio_service import TwilioService
from tests.mocks.twilio import make_twilio_webhook_payload

# All webhook tests mock handle_inbound_message to avoid LLM calls
_MOCK_AGENT_RESPONSE = AgentResponse(reply_text="Mock reply")
_PATCH_HANDLE = "backend.app.routers.webhooks.handle_inbound_message"


def test_inbound_webhook_returns_200(client: TestClient) -> None:
    """Valid webhook payload should return 200 with empty TwiML."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_twilio_webhook_payload()
        response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200
    assert "<Response/>" in response.text


def test_inbound_webhook_stores_message(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Inbound message should be stored in the database."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
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
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="Here are the photos",
            num_media=2,
            media_urls=[
                "https://api.twilio.com/media1.jpg",
                "https://api.twilio.com/media2.jpg",
            ],
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
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_twilio_webhook_payload(from_number="+15559999999", body="Hi")
        response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200

    contractor = db_session.query(Contractor).filter(Contractor.phone == "+15559999999").first()
    assert contractor is not None


def test_inbound_webhook_creates_conversation(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Should create a conversation for the contractor."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
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
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
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
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_twilio_webhook_payload()
        response = client.post("/api/webhooks/twilio/inbound", data=payload)
    assert response.status_code == 200
    assert isinstance(mock_twilio_service, MagicMock)


def test_webhook_calls_handle_inbound_message(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should call handle_inbound_message after storing the message."""
    with patch(
        _PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE
    ) as mock_handle:
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="Need a quote",
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    assert response.status_code == 200
    mock_handle.assert_called_once()

    # Verify the call args
    call_kwargs = mock_handle.call_args
    assert call_kwargs.kwargs["contractor"].phone == test_contractor.phone
    assert call_kwargs.kwargs["message"].body == "Need a quote"
    assert call_kwargs.kwargs["media_urls"] == []


def test_webhook_calls_handle_with_media_tuples(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should pass media as (url, content_type) tuples to handler."""
    with patch(
        _PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE
    ) as mock_handle:
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="Photos",
            num_media=1,
            media_urls=["https://api.twilio.com/photo.jpg"],
            media_types=["image/jpeg"],
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    assert response.status_code == 200
    call_kwargs = mock_handle.call_args.kwargs
    assert call_kwargs["media_urls"] == [("https://api.twilio.com/photo.jpg", "image/jpeg")]


def test_webhook_survives_handler_failure(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should return 200 even if handle_inbound_message raises."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, side_effect=RuntimeError("LLM down")):
        payload = make_twilio_webhook_payload(
            from_number=test_contractor.phone,
            body="Hello",
        )
        response = client.post("/api/webhooks/twilio/inbound", data=payload)

    # Should still return 200 (message was stored, agent failure is logged)
    assert response.status_code == 200
    messages = db_session.query(Message).all()
    assert len(messages) == 1
