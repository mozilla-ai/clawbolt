"""Tests for Telegram webhook endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.core import AgentResponse
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.telegram import make_telegram_update_payload

# All webhook tests mock handle_inbound_message to avoid LLM calls
_MOCK_AGENT_RESPONSE = AgentResponse(reply_text="Mock reply")
_PATCH_HANDLE = "backend.app.routers.telegram_webhook.handle_inbound_message"


def test_inbound_webhook_returns_200(client: TestClient) -> None:
    """Valid webhook payload should return 200 with ok:true."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(chat_id=123456789, text="Hello")
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_inbound_webhook_stores_message(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Inbound message should be stored in the database."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="I need a quote for kitchen remodel",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].direction == "inbound"
    assert messages[0].body == "I need a quote for kitchen remodel"


def test_inbound_webhook_extracts_photo(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Photo file_ids should be extracted and stored."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="Here are the photos",
            photo_file_id="AgACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "AgACAgIAAxkBAAI" in messages[0].media_urls_json


def test_inbound_webhook_extracts_voice(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Voice file_ids should be extracted."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="",
            voice_file_id="AwACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "AwACAgIAAxkBAAI" in messages[0].media_urls_json


def test_inbound_webhook_extracts_document(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Document file_ids should be extracted."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="",
            document_file_id="BQACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "BQACAgIAAxkBAAI" in messages[0].media_urls_json


def test_inbound_webhook_creates_contractor_if_new(client: TestClient, db_session: Session) -> None:
    """Unknown chat_id should create a new contractor."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(chat_id=999999999, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    contractor = (
        db_session.query(Contractor).filter(Contractor.channel_identifier == "999999999").first()
    )
    assert contractor is not None
    assert contractor.preferred_channel == "telegram"


def test_inbound_webhook_creates_conversation(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Should create a conversation for the contractor."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier), text="Hello"
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    conversations = (
        db_session.query(Conversation)
        .filter(Conversation.contractor_id == test_contractor.id)
        .all()
    )
    assert len(conversations) == 1
    assert conversations[0].is_active is True


def test_messaging_service_injected_via_depends(
    client: TestClient, mock_messaging_service: MessagingService
) -> None:
    """MessagingService should be injected via Depends and overridable in tests."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload()
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert isinstance(mock_messaging_service, MagicMock)


def test_webhook_calls_handle_inbound_message(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should call handle_inbound_message via background task."""
    with patch(
        _PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE
    ) as mock_handle:
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="Need a quote",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_handle.assert_called_once()

    call_kwargs = mock_handle.call_args
    assert call_kwargs.kwargs["contractor"].channel_identifier == test_contractor.channel_identifier
    assert call_kwargs.kwargs["message"].body == "Need a quote"


def test_webhook_calls_handle_with_media(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should pass media as (file_id, mime_type) tuples to handler."""
    with patch(
        _PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE
    ) as mock_handle:
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="Photos",
            photo_file_id="AgACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    call_kwargs = mock_handle.call_args.kwargs
    assert call_kwargs["media_urls"] == [("AgACAgIAAxkBAAI", "image/jpeg")]


def test_webhook_survives_handler_failure(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Webhook should return 200 even if handle_inbound_message raises."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, side_effect=RuntimeError("LLM down")):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="Hello",
        )
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    messages = db_session.query(Message).all()
    assert len(messages) == 1


def test_webhook_idempotency_skips_duplicate(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Duplicate webhook calls should not create duplicate messages."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="First message",
            message_id=999,
        )
        response1 = client.post("/api/webhooks/telegram", json=payload)
        response2 = client.post("/api/webhooks/telegram", json=payload)

    assert response1.status_code == 200
    assert response2.status_code == 200

    messages = db_session.query(Message).filter(Message.direction == "inbound").all()
    assert len(messages) == 1
    chat_id = test_contractor.channel_identifier
    assert messages[0].external_message_id == f"tg_{chat_id}_999"


def test_webhook_non_message_update_returns_200(client: TestClient) -> None:
    """Non-message updates (e.g., edited_message) should return 200 without processing."""
    payload = {"update_id": 200, "edited_message": {"text": "edited"}}
    response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# -- Allowlist gating tests --


def test_allowlist_rejects_unlisted_chat_id(client: TestClient, db_session: Session) -> None:
    """Messages from a chat_id not in the allowlist should be silently ignored."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "111,222",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=999, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_not_called()
    assert db_session.query(Message).count() == 0


def test_allowlist_accepts_listed_chat_id(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Messages from a chat_id on the allowlist should be processed normally."""
    chat_id = test_contractor.channel_identifier
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            f"111,{chat_id},333",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=int(chat_id), text="Hello")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_called_once()
    assert db_session.query(Message).count() == 1


def test_allowlist_empty_allows_all(client: TestClient, db_session: Session) -> None:
    """Empty allowlist (default) should allow all chat IDs."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=777777, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_called_once()


# -- Username allowlist tests --


def test_username_allowlist_accepts_listed_user(client: TestClient, db_session: Session) -> None:
    """Messages from a username on the allowlist should be processed."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "alice,bob",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=555, text="Hi", username="alice")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_called_once()


def test_username_allowlist_rejects_unlisted_user(client: TestClient, db_session: Session) -> None:
    """Messages from a username NOT on the allowlist should be ignored."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "alice,bob",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=555, text="Hi", username="eve")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_not_called()
    assert db_session.query(Message).count() == 0


def test_username_allowlist_strips_at_prefix(client: TestClient, db_session: Session) -> None:
    """Usernames with @ prefix in config should still match."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "@Alice, @Bob",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=555, text="Hi", username="alice")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_called_once()


def test_username_or_chat_id_either_passes(client: TestClient, db_session: Session) -> None:
    """If either chat_id OR username matches, the message should be allowed."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "999",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "alice",
        ),
    ):
        # chat_id doesn't match (555 != 999), but username matches
        payload = make_telegram_update_payload(chat_id=555, text="Hi", username="alice")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_called_once()


def test_username_allowlist_no_username_in_payload(client: TestClient, db_session: Session) -> None:
    """Messages without a username should be rejected when only username allowlist is set."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "alice",
        ),
    ):
        # No username in payload
        payload = make_telegram_update_payload(chat_id=555, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_h.assert_not_called()
    assert db_session.query(Message).count() == 0
