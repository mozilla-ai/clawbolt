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
_PATCH_HANDLE = "backend.app.agent.ingestion.handle_inbound_message"


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


def test_allowlist_empty_denies_all(client: TestClient, db_session: Session) -> None:
    """Empty allowlist (default) should deny all chat IDs."""
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
    mock_h.assert_not_called()
    assert db_session.query(Message).count() == 0


def test_allowlist_wildcard_allows_all(client: TestClient, db_session: Session) -> None:
    """Setting TELEGRAM_ALLOWED_CHAT_IDS to '*' should allow all chat IDs."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "*",
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


def test_username_wildcard_allows_all(client: TestClient, db_session: Session) -> None:
    """Setting TELEGRAM_ALLOWED_USERNAMES to '*' should allow all users."""
    with (
        patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h,
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_chat_ids",
            "",
        ),
        patch(
            "backend.app.routers.telegram_webhook.settings.telegram_allowed_usernames",
            "*",
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


# -- Regression tests for document MIME classification --


def test_webhook_invalid_json_returns_200(client: TestClient) -> None:
    """Invalid JSON body should return 200 without crashing."""
    response = client.post(
        "/api/webhooks/telegram",
        content=b"not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_missing_chat_id_returns_200(client: TestClient) -> None:
    """Message without chat.id should return 200 without crashing."""
    payload = {"update_id": 1, "message": {"text": "hello"}}
    response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_extract_media_skips_photo_without_file_id() -> None:
    """Photos missing file_id should be skipped instead of crashing."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "photo": [{"file_unique_id": "abc", "width": 90, "height": 90, "file_size": 1000}],
        }
    }
    media = _extract_telegram_media(update)
    assert media == []


def test_extract_media_skips_voice_without_file_id() -> None:
    """Voice notes missing file_id should be skipped."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "voice": {"file_unique_id": "v1", "duration": 5},
        }
    }
    media = _extract_telegram_media(update)
    assert media == []


def test_extract_media_skips_document_without_file_id() -> None:
    """Documents missing file_id should be skipped."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "document": {"file_unique_id": "d1", "file_name": "test.pdf"},
        }
    }
    media = _extract_telegram_media(update)
    assert media == []


def test_extract_telegram_media_image_document_preserves_mime() -> None:
    """Images sent as documents should preserve their image/* MIME type."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "message_id": 1,
            "chat": {"id": 123},
            "document": {
                "file_id": "BQACAgIAAxkBAAI",
                "file_unique_id": "doc1",
                "file_name": "screenshot.png",
                "mime_type": "image/png",
            },
        }
    }
    media = _extract_telegram_media(update)
    assert len(media) == 1
    assert media[0] == ("BQACAgIAAxkBAAI", "image/png")


def test_extract_telegram_media_document_without_mime_defaults() -> None:
    """Documents without mime_type should default to application/octet-stream."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "message_id": 1,
            "chat": {"id": 123},
            "document": {
                "file_id": "BQACAgIAAxkBAAI",
                "file_unique_id": "doc1",
                "file_name": "unknown_file",
            },
        }
    }
    media = _extract_telegram_media(update)
    assert len(media) == 1
    assert media[0] == ("BQACAgIAAxkBAAI", "application/octet-stream")


# -- Regression: caption extraction for media messages --


def test_parse_photo_with_caption_extracts_text(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Photo messages with a caption should store the caption as body."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            photo_file_id="AgACAgIAAxkBAAI",
            caption="Kitchen remodel damage",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].body == "Kitchen remodel damage"
    assert "AgACAgIAAxkBAAI" in messages[0].media_urls_json


def test_parse_document_with_caption_extracts_text(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Document messages with a caption should store the caption as body."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            document_file_id="BQACAgIAAxkBAAI",
            caption="Invoice for deck job",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].body == "Invoice for deck job"


def test_parse_media_without_caption_has_empty_body(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Media messages without a caption should have an empty body."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            photo_file_id="AgACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].body == ""


# -- Regression: missing Telegram media types --


def test_extract_media_video() -> None:
    """Video file_ids should be extracted."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "video": {
                "file_id": "BAACAgIAAxkBAAI",
                "file_unique_id": "vid1",
                "duration": 10,
                "width": 1280,
                "height": 720,
                "mime_type": "video/mp4",
            }
        }
    }
    media = _extract_telegram_media(update)
    assert len(media) == 1
    assert media[0] == ("BAACAgIAAxkBAAI", "video/mp4")


def test_extract_media_video_note() -> None:
    """Video note (round video) file_ids should be extracted."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "video_note": {
                "file_id": "DQACAgIAAxkBAAI",
                "file_unique_id": "vnote1",
                "duration": 5,
                "length": 240,
            }
        }
    }
    media = _extract_telegram_media(update)
    assert len(media) == 1
    assert media[0] == ("DQACAgIAAxkBAAI", "video/mp4")


def test_extract_media_audio() -> None:
    """Audio file_ids should be extracted."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {
        "message": {
            "audio": {
                "file_id": "CQACAgIAAxkBAAI",
                "file_unique_id": "audio1",
                "duration": 180,
                "mime_type": "audio/mpeg",
            }
        }
    }
    media = _extract_telegram_media(update)
    assert len(media) == 1
    assert media[0] == ("CQACAgIAAxkBAAI", "audio/mpeg")


def test_extract_media_video_without_file_id() -> None:
    """Videos missing file_id should be skipped."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {"message": {"video": {"file_unique_id": "v1", "duration": 10}}}
    media = _extract_telegram_media(update)
    assert media == []


def test_extract_media_video_note_without_file_id() -> None:
    """Video notes missing file_id should be skipped."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {"message": {"video_note": {"file_unique_id": "vn1", "duration": 5}}}
    media = _extract_telegram_media(update)
    assert media == []


def test_extract_media_audio_without_file_id() -> None:
    """Audio files missing file_id should be skipped."""
    from backend.app.routers.telegram_webhook import _extract_telegram_media

    update = {"message": {"audio": {"file_unique_id": "a1", "duration": 180}}}
    media = _extract_telegram_media(update)
    assert media == []


def test_inbound_webhook_extracts_video(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """Video file_ids should be extracted and stored."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            video_file_id="BAACAgIAAxkBAAI",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert "BAACAgIAAxkBAAI" in messages[0].media_urls_json


# -- Telegram bot command handling --


def test_start_command_converted_to_greeting(
    client: TestClient, db_session: Session, test_contractor: Contractor
) -> None:
    """/start command should be converted to a greeting, not passed as raw text."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE):
        payload = make_telegram_update_payload(
            chat_id=int(test_contractor.channel_identifier),
            text="/start",
        )
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200

    messages = db_session.query(Message).all()
    assert len(messages) == 1
    assert messages[0].body == "Hi"


def test_other_bot_commands_ignored(client: TestClient, db_session: Session) -> None:
    """Unhandled bot commands (e.g. /help) should be silently ignored."""
    with patch(_PATCH_HANDLE, new_callable=AsyncMock, return_value=_MOCK_AGENT_RESPONSE) as mock_h:
        payload = make_telegram_update_payload(chat_id=123456789, text="/help")
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    mock_h.assert_not_called()
    assert db_session.query(Message).count() == 0
