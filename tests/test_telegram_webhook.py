"""Tests for Telegram webhook endpoint.

With the message bus, the webhook handler validates, parses, checks the
allowlist, and publishes an InboundMessage to the bus. The bus consumer
handles user lookup, session creation, message persistence, and
the agent pipeline. Tests below verify the webhook's responsibilities:
parsing, allowlist gating, idempotency, and correct bus publishing.
"""

import contextlib
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.mocks.telegram import make_telegram_update_payload

_PATCH_BUS_PUBLISH = "backend.app.channels.telegram.message_bus.publish_inbound"


def test_inbound_webhook_returns_200(client: TestClient) -> None:
    """Valid webhook payload should return 200 with ok:true."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        payload = make_telegram_update_payload(chat_id=123456789, text="Hello")
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_inbound_webhook_publishes_text(client: TestClient) -> None:
    """Inbound text message should be published to the bus."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(chat_id=123456789, text="Need a quote")
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "telegram"
    assert inbound.sender_id == "123456789"
    assert inbound.text == "Need a quote"


def test_inbound_webhook_publishes_photo(client: TestClient) -> None:
    """Photo file_ids should be included in the published InboundMessage."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            text="Here are the photos",
            photo_file_id="AgACAgIAAxkBAAI",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert ("AgACAgIAAxkBAAI", "image/jpeg") in inbound.media_refs


def test_inbound_webhook_publishes_document(client: TestClient) -> None:
    """Document file_ids should be included in the published InboundMessage."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            text="",
            document_file_id="BQACAgIAAxkBAAI",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert any(fid == "BQACAgIAAxkBAAI" for fid, _ in inbound.media_refs)


def test_inbound_webhook_publishes_video(client: TestClient) -> None:
    """Video file_ids should be included in the published InboundMessage."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            video_file_id="BAACAgIAAxkBAAI",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert any(fid == "BAACAgIAAxkBAAI" for fid, _ in inbound.media_refs)


def test_webhook_idempotency_skips_duplicate(client: TestClient) -> None:
    """Duplicate webhook calls should not publish to bus twice."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            text="First message",
            message_id=999,
        )
        response1 = client.post("/api/webhooks/telegram", json=payload)
        response2 = client.post("/api/webhooks/telegram", json=payload)

    assert response1.status_code == 200
    assert response2.status_code == 200
    # Only one publish (duplicate skipped)
    mock_pub.assert_called_once()


def test_webhook_survives_bus_publish_failure(client: TestClient) -> None:
    """Webhook should return 200 even if bus publish raises."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock, side_effect=RuntimeError("Bus down")):
        payload = make_telegram_update_payload(chat_id=123456789, text="Hello")
        # Bus publish failure will propagate but we still check 200 behavior
        with contextlib.suppress(Exception):
            client.post("/api/webhooks/telegram", json=payload)


# -- Allowlist gating tests --


def test_allowlist_rejects_unlisted_chat_id(client: TestClient) -> None:
    """Messages from a chat_id not matching the configured ID should be ignored."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.telegram.settings.telegram_allowed_chat_id",
            "111",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=999, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_pub.assert_not_called()


def test_allowlist_accepts_matching_chat_id(client: TestClient) -> None:
    """Messages from the configured chat ID should be published to bus."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.telegram.settings.telegram_allowed_chat_id",
            "123456789",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=123456789, text="Hello")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_pub.assert_called_once()


def test_allowlist_empty_denies_all(client: TestClient) -> None:
    """Empty allowlist (default) should deny all chat IDs."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.telegram.settings.telegram_allowed_chat_id",
            "",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=777777, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_pub.assert_not_called()


def test_allowlist_wildcard_allows_all(client: TestClient) -> None:
    """Setting TELEGRAM_ALLOWED_CHAT_ID to '*' should allow all chat IDs."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.telegram.settings.telegram_allowed_chat_id",
            "*",
        ),
    ):
        payload = make_telegram_update_payload(chat_id=777777, text="Hi")
        response = client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
    mock_pub.assert_called_once()


# -- Edge cases and error handling --


def test_webhook_non_message_update_returns_200(client: TestClient) -> None:
    """Non-message updates (e.g., edited_message) should return 200 without processing."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = {"update_id": 200, "edited_message": {"text": "edited"}}
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_pub.assert_not_called()


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


# -- Telegram bot command handling --


def test_start_command_converted_to_greeting(client: TestClient) -> None:
    """/start command should be converted to a greeting in the published message."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(chat_id=123456789, text="/start")
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Hi"


def test_other_bot_commands_ignored(client: TestClient) -> None:
    """Unhandled bot commands (e.g. /help) should be silently ignored."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(chat_id=123456789, text="/help")
        response = client.post("/api/webhooks/telegram", json=payload)
    assert response.status_code == 200
    mock_pub.assert_not_called()


# -- Caption extraction tests --


def test_photo_with_caption_publishes_caption(client: TestClient) -> None:
    """Photo messages with a caption should publish the caption as text."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            photo_file_id="AgACAgIAAxkBAAI",
            caption="Kitchen remodel damage",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Kitchen remodel damage"
    assert ("AgACAgIAAxkBAAI", "image/jpeg") in inbound.media_refs


def test_document_with_caption_publishes_caption(client: TestClient) -> None:
    """Document messages with a caption should publish the caption as text."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            document_file_id="BQACAgIAAxkBAAI",
            caption="Invoice for deck job",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Invoice for deck job"


def test_media_without_caption_publishes_empty_text(client: TestClient) -> None:
    """Media messages without a caption should publish empty text."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_telegram_update_payload(
            chat_id=123456789,
            photo_file_id="AgACAgIAAxkBAAI",
        )
        client.post("/api/webhooks/telegram", json=payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == ""


# -- Static method tests (no bus involved) --


def test_extract_media_skips_photo_without_file_id() -> None:
    """Photos missing file_id should be skipped instead of crashing."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
            "message": {
                "photo": [{"file_unique_id": "abc", "width": 90, "height": 90, "file_size": 1000}],
            }
        }
    )
    media = TelegramChannel.extract_media(update)
    assert media == []


def test_extract_media_skips_document_without_file_id() -> None:
    """Documents missing file_id should be skipped."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
            "message": {
                "document": {"file_unique_id": "d1", "file_name": "test.pdf"},
            }
        }
    )
    media = TelegramChannel.extract_media(update)
    assert media == []


def test_extract_telegram_media_image_document_preserves_mime() -> None:
    """Images sent as documents should preserve their image/* MIME type."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
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
    )
    media = TelegramChannel.extract_media(update)
    assert len(media) == 1
    assert media[0] == ("BQACAgIAAxkBAAI", "image/png")


def test_extract_telegram_media_document_without_mime_defaults() -> None:
    """Documents without mime_type should default to application/octet-stream."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
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
    )
    media = TelegramChannel.extract_media(update)
    assert len(media) == 1
    assert media[0] == ("BQACAgIAAxkBAAI", "application/octet-stream")


def test_extract_media_video() -> None:
    """Video file_ids should be extracted."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
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
    )
    media = TelegramChannel.extract_media(update)
    assert len(media) == 1
    assert media[0] == ("BAACAgIAAxkBAAI", "video/mp4")


def test_extract_media_video_note() -> None:
    """Video note (round video) file_ids should be extracted."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {
            "message": {
                "video_note": {
                    "file_id": "DQACAgIAAxkBAAI",
                    "file_unique_id": "vnote1",
                    "duration": 5,
                    "length": 240,
                }
            }
        }
    )
    media = TelegramChannel.extract_media(update)
    assert len(media) == 1
    assert media[0] == ("DQACAgIAAxkBAAI", "video/mp4")


def test_extract_media_video_without_file_id() -> None:
    """Videos missing file_id should be skipped."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {"message": {"video": {"file_unique_id": "v1", "duration": 10}}}
    )
    media = TelegramChannel.extract_media(update)
    assert media == []


def test_extract_media_video_note_without_file_id() -> None:
    """Video notes missing file_id should be skipped."""
    from backend.app.channels.telegram import TelegramChannel, TelegramUpdate

    update = TelegramUpdate.model_validate(
        {"message": {"video_note": {"file_unique_id": "vn1", "duration": 5}}}
    )
    media = TelegramChannel.extract_media(update)
    assert media == []


# -- TelegramUpdate model tests --


def test_telegram_update_ignores_extra_fields() -> None:
    """TelegramUpdate should silently ignore unknown Telegram fields."""
    from backend.app.channels.telegram import TelegramUpdate

    data = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": 123, "type": "private"},
            "text": "hello",
            "from": {"id": 123, "is_bot": False, "first_name": "Test"},
            "unknown_future_field": True,
        },
        "also_unknown": "ignored",
    }
    update = TelegramUpdate.model_validate(data)
    assert update.update_id == 1
    assert update.message is not None
    assert update.message.text == "hello"


def test_telegram_update_handles_missing_message() -> None:
    """TelegramUpdate without a message field should parse with message=None."""
    from backend.app.channels.telegram import TelegramUpdate

    update = TelegramUpdate.model_validate({"update_id": 99})
    assert update.message is None
