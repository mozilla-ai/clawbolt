"""Tests for BlueBubbles channel: webhook handling and outbound messaging.

Mirrors the structure of test_linq_channel.py. The webhook handler
validates the password query param, parses payloads, checks the allowlist,
and publishes InboundMessages to the bus.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.channels.bluebubbles import BlueBubblesChannel
from tests.mocks.bluebubbles import make_bluebubbles_webhook_payload

_PATCH_BUS_PUBLISH = "backend.app.bus.message_bus.publish_inbound"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_webhook(
    client: TestClient,
    payload: dict,
    password: str = "",
) -> httpx.Response:
    """Post a BlueBubbles webhook with optional password query param."""
    url = "/api/webhooks/bluebubbles"
    if password:
        url = f"{url}?password={password}"
    return client.post(url, json=payload)


# ---------------------------------------------------------------------------
# Webhook endpoint tests
# ---------------------------------------------------------------------------


def test_inbound_webhook_returns_200(bluebubbles_client: TestClient) -> None:
    """Valid webhook payload should return 200 with ok:true."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        payload = make_bluebubbles_webhook_payload(text="Hello")
        resp = _post_webhook(bluebubbles_client, payload)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_inbound_webhook_publishes_text(bluebubbles_client: TestClient) -> None:
    """Inbound text message should be published to the bus."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_bluebubbles_webhook_payload(sender="+15559876543", text="Need a quote")
        _post_webhook(bluebubbles_client, payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "bluebubbles"
    assert inbound.sender_id == "+15559876543"
    assert inbound.text == "Need a quote"


def test_inbound_webhook_publishes_media(bluebubbles_client: TestClient) -> None:
    """Media messages should include media_refs in the published InboundMessage."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_bluebubbles_webhook_payload(
            text="Check this out",
            attachments=[
                {
                    "guid": "att-guid-001",
                    "mimeType": "image/jpeg",
                    "transferName": "photo.jpg",
                    "totalBytes": 12345,
                }
            ],
        )
        _post_webhook(bluebubbles_client, payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Check this out"
    assert len(inbound.media_refs) == 1
    assert inbound.media_refs[0][0] == "att-guid-001"
    assert inbound.media_refs[0][1] == "image/jpeg"


def test_is_from_me_ignored(bluebubbles_client: TestClient) -> None:
    """Messages sent by the Mac (isFromMe=True) should be ignored."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_bluebubbles_webhook_payload(text="Echo", is_from_me=True)
        resp = _post_webhook(bluebubbles_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_non_new_message_event_ignored(bluebubbles_client: TestClient) -> None:
    """Non-new-message events should return 200 without publishing."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        for event in ("typing-indicator", "chat-read-status-changed", "updated-message"):
            payload = make_bluebubbles_webhook_payload(text="Hi", event_type=event)
            resp = _post_webhook(bluebubbles_client, payload)
            assert resp.status_code == 200

    mock_pub.assert_not_called()


def test_invalid_json_returns_200(bluebubbles_client: TestClient) -> None:
    """Invalid JSON body should return 200 without crashing."""
    resp = bluebubbles_client.post(
        "/api/webhooks/bluebubbles",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_duplicate_message_skipped(bluebubbles_client: TestClient) -> None:
    """Duplicate webhook calls should not publish to bus twice."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_bluebubbles_webhook_payload(text="First", message_guid="dup-msg-001")
        _post_webhook(bluebubbles_client, payload)
        _post_webhook(bluebubbles_client, payload)

    mock_pub.assert_called_once()


def test_invalid_password_does_not_publish(bluebubbles_client: TestClient) -> None:
    """Invalid password should return 200 but not publish to bus."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_password",
            "correct-password",
        ),
    ):
        payload = make_bluebubbles_webhook_payload(text="Hi")
        resp = _post_webhook(bluebubbles_client, payload, password="wrong-password")

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_correct_password_publishes(bluebubbles_client: TestClient) -> None:
    """Correct password should publish to bus."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_password",
            "correct-password",
        ),
    ):
        payload = make_bluebubbles_webhook_payload(text="Hi")
        resp = _post_webhook(bluebubbles_client, payload, password="correct-password")

    assert resp.status_code == 200
    mock_pub.assert_called_once()


# ---------------------------------------------------------------------------
# Allowlist tests
# ---------------------------------------------------------------------------


def test_allowlist_empty_denies_all(bluebubbles_client: TestClient) -> None:
    """Empty allowlist should deny all senders."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", ""),
    ):
        payload = make_bluebubbles_webhook_payload(sender="+15551234567", text="Hi")
        resp = _post_webhook(bluebubbles_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_allowlist_wildcard_allows_all(bluebubbles_client: TestClient) -> None:
    """Setting allowed_numbers to '*' should allow all senders."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
    ):
        payload = make_bluebubbles_webhook_payload(sender="+15559999999", text="Hi")
        resp = _post_webhook(bluebubbles_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_called_once()


def test_allowlist_matching_number_allows(bluebubbles_client: TestClient) -> None:
    """Matching E.164 number should be allowed."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers",
            "+15551234567",
        ),
    ):
        payload = make_bluebubbles_webhook_payload(sender="+15551234567", text="Hi")
        resp = _post_webhook(bluebubbles_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_called_once()


def test_allowlist_non_matching_number_denies(bluebubbles_client: TestClient) -> None:
    """Non-matching phone number should be denied."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers",
            "+15551234567",
        ),
    ):
        payload = make_bluebubbles_webhook_payload(sender="+15559999999", text="Hi")
        resp = _post_webhook(bluebubbles_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# is_allowed() unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_is_allowed_empty_denies() -> None:
    channel = BlueBubblesChannel()
    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", ""):
        assert channel.is_allowed("+15551234567", "") is False


def test_is_allowed_wildcard_allows() -> None:
    channel = BlueBubblesChannel()
    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"):
        assert channel.is_allowed("+15551234567", "") is True


def test_is_allowed_matching_number() -> None:
    channel = BlueBubblesChannel()
    with patch(
        "backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "+15551234567"
    ):
        assert channel.is_allowed("+15551234567", "") is True
        assert channel.is_allowed("+15559999999", "") is False


def test_is_allowed_premium_override() -> None:
    """When premium override is set, it should take precedence."""
    channel = BlueBubblesChannel()
    with patch.object(channel, "_check_premium_route", return_value=True):
        assert channel.is_allowed("+15551234567", "") is True
    with patch.object(channel, "_check_premium_route", return_value=False):
        assert channel.is_allowed("+15551234567", "") is False


# ---------------------------------------------------------------------------
# Outbound tests (mock httpx)
# ---------------------------------------------------------------------------


def _make_mock_http(json_response: dict | None = None) -> AsyncMock:
    """Create a mock httpx.AsyncClient with common setup."""
    mock = AsyncMock()
    if json_response is not None:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = lambda: None
        mock_resp.json = lambda: json_response
        mock_resp.content = b""
        mock_resp.headers = {}
        mock.post.return_value = mock_resp
        mock.get.return_value = mock_resp
    return mock


async def test_send_text_with_cached_chat_guid() -> None:
    """send_text should use cached chat_guid for existing conversations."""
    channel = BlueBubblesChannel()
    channel._chat_cache["+15551234567"] = "iMessage;-;+15551234567"

    mock_http = _make_mock_http({"guid": "resp-msg-001"})
    channel._client = mock_http

    result = await channel.send_text("+15551234567", "Hello there")

    assert result == "resp-msg-001"
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "/api/v1/message/text"
    body = call_args[1]["json"]
    assert body["chatGuid"] == "iMessage;-;+15551234567"
    assert body["message"] == "Hello there"


async def test_send_text_constructs_chat_guid() -> None:
    """send_text should construct chat GUID when no cached value exists."""
    channel = BlueBubblesChannel()

    mock_http = _make_mock_http({"guid": "new-msg-001"})
    channel._client = mock_http

    await channel.send_text("+15559876543", "Hello")

    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    body = call_args[1]["json"]
    assert body["chatGuid"] == "iMessage;-;+15559876543"


async def test_send_text_includes_temp_guid() -> None:
    """send_text must include tempGuid (required by BlueBubbles AppleScript sending)."""
    channel = BlueBubblesChannel()

    mock_http = _make_mock_http({"guid": "msg-001"})
    channel._client = mock_http

    await channel.send_text("+15551234567", "Hi")

    body = mock_http.post.call_args[1]["json"]
    assert "tempGuid" in body
    assert body["tempGuid"].startswith("temp-")


async def test_send_media_includes_temp_guid() -> None:
    """send_media must include tempGuid (required by BlueBubbles AppleScript sending)."""
    channel = BlueBubblesChannel()
    channel._chat_cache["+15551234567"] = "iMessage;-;+15551234567"

    mock_http = _make_mock_http({"guid": "media-001"})
    channel._client = mock_http

    mock_dl_resp = AsyncMock()
    mock_dl_resp.content = b"fake-image"
    mock_dl_resp.headers = {"content-type": "image/jpeg"}
    mock_dl_resp.raise_for_status = lambda: None

    with patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_dl_cls:
        mock_dl_client = AsyncMock()
        mock_dl_client.__aenter__ = AsyncMock(return_value=mock_dl_client)
        mock_dl_client.__aexit__ = AsyncMock(return_value=False)
        mock_dl_client.get.return_value = mock_dl_resp
        mock_dl_cls.return_value = mock_dl_client

        await channel.send_media("+15551234567", "Photo", "https://example.com/pic.jpg")

    data = mock_http.post.call_args[1]["data"]
    assert "tempGuid" in data
    assert data["tempGuid"].startswith("temp-")


async def test_send_typing_indicator_private_api() -> None:
    """send_typing_indicator should POST when using private-api method."""
    channel = BlueBubblesChannel()
    channel._chat_cache["+15551234567"] = "iMessage;-;+15551234567"

    mock_http = _make_mock_http({})
    channel._client = mock_http

    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_send_method", "private-api"):
        await channel.send_typing_indicator("+15551234567")

    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "/api/v1/chat/iMessage;-;+15551234567/typing"


async def test_send_typing_indicator_skipped_for_apple_script() -> None:
    """send_typing_indicator should be a no-op when using apple-script method."""
    channel = BlueBubblesChannel()

    mock_http = _make_mock_http({})
    channel._client = mock_http

    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_send_method", "apple-script"):
        await channel.send_typing_indicator("+15551234567")

    mock_http.post.assert_not_called()


async def test_send_typing_indicator_no_error_on_failure() -> None:
    """send_typing_indicator should not raise on failure."""
    channel = BlueBubblesChannel()

    mock_http = AsyncMock()
    mock_http.post.side_effect = Exception("Connection refused")
    channel._client = mock_http

    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_send_method", "private-api"):
        # Should not raise
        await channel.send_typing_indicator("+15551234567")


async def test_send_media_multipart_upload() -> None:
    """send_media should download media then upload as multipart form data."""
    channel = BlueBubblesChannel()
    channel._chat_cache["+15551234567"] = "iMessage;-;+15551234567"

    # Mock the BB server response for the upload
    mock_http = _make_mock_http({"guid": "media-msg-001"})
    channel._client = mock_http

    # Mock the media download from the source URL
    mock_dl_resp = AsyncMock()
    mock_dl_resp.content = b"fake-image-bytes"
    mock_dl_resp.headers = {"content-type": "image/jpeg"}
    mock_dl_resp.raise_for_status = lambda: None

    with patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_dl_cls:
        mock_dl_client = AsyncMock()
        mock_dl_client.__aenter__ = AsyncMock(return_value=mock_dl_client)
        mock_dl_client.__aexit__ = AsyncMock(return_value=False)
        mock_dl_client.get.return_value = mock_dl_resp
        mock_dl_cls.return_value = mock_dl_client

        result = await channel.send_media(
            "+15551234567", "Check this", "https://example.com/photo.jpg"
        )

    assert result == "media-msg-001"
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "/api/v1/message/attachment"
    assert call_args[1]["data"]["chatGuid"] == "iMessage;-;+15551234567"
    assert call_args[1]["data"]["message"] == "Check this"


async def test_send_message_multiple_media() -> None:
    """send_message with multiple media URLs should send each one."""
    channel = BlueBubblesChannel()
    channel._chat_cache["+15551234567"] = "iMessage;-;+15551234567"

    mock_http = _make_mock_http({"guid": "multi-msg-001"})
    channel._client = mock_http

    with patch.object(channel, "send_media", new_callable=AsyncMock) as mock_send_media:
        mock_send_media.return_value = "multi-msg-001"
        result = await channel.send_message(
            "+15551234567", "Two pics", ["https://example.com/a.jpg", "https://example.com/b.jpg"]
        )

    assert result == "multi-msg-001"
    assert mock_send_media.call_count == 2
    mock_send_media.assert_any_call("+15551234567", "Two pics", "https://example.com/a.jpg")
    mock_send_media.assert_any_call("+15551234567", "", "https://example.com/b.jpg")


async def test_download_media_size_limit_exceeded() -> None:
    """download_media should raise ValueError when file exceeds size limit."""
    channel = BlueBubblesChannel()

    mock_http = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.content = b"x" * 100
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.raise_for_status = lambda: None
    mock_http.get.return_value = mock_resp
    channel._client = mock_http

    with (
        patch("backend.app.media.download.settings.max_media_size_bytes", 50),
        pytest.raises(ValueError, match="too large"),
    ):
        await channel.download_media("att-guid-too-large")


async def test_download_media() -> None:
    """download_media should fetch content from the BlueBubbles API."""
    channel = BlueBubblesChannel()

    mock_http = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.content = b"fake-image-data"
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.raise_for_status = lambda: None
    mock_http.get.return_value = mock_resp
    channel._client = mock_http

    result = await channel.download_media("att-guid-001")

    assert result.content == b"fake-image-data"
    assert result.mime_type == "image/jpeg"
    assert result.original_url == "att-guid-001"
    mock_http.get.assert_called_once()
    call_args = mock_http.get.call_args
    assert call_args[0][0] == "/api/v1/attachment/att-guid-001/download"


# ---------------------------------------------------------------------------
# Chat cache population test
# ---------------------------------------------------------------------------


def test_webhook_populates_chat_cache(bluebubbles_client: TestClient) -> None:
    """Inbound webhook should populate the chat cache for outbound use."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        payload = make_bluebubbles_webhook_payload(
            sender="+15551234567",
            text="Hello",
            chat_guid="iMessage;-;+15551234567",
        )
        _post_webhook(bluebubbles_client, payload)

    from backend.app.channels import get_channel

    channel = get_channel("bluebubbles")
    assert isinstance(channel, BlueBubblesChannel)
    assert channel._chat_cache.get("+15551234567") == "iMessage;-;+15551234567"


# ---------------------------------------------------------------------------
# Webhook auto-registration tests
# ---------------------------------------------------------------------------


async def test_register_bluebubbles_webhook_success() -> None:
    """register_bluebubbles_webhook should POST to /api/v1/webhook and return True."""
    from backend.app.channels.bluebubbles import register_bluebubbles_webhook

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"id": 1}
    mock_resp.text = ""

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.settings.http_timeout_seconds", 10),
        patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await register_bluebubbles_webhook(
            "https://my-mac.example.com",
            "https://tunnel.example.com/api/webhooks/bluebubbles",
        )

    assert result is True
    mock_client.post.assert_called_once()


async def test_register_bluebubbles_webhook_failure() -> None:
    """register_bluebubbles_webhook should return False on HTTP error."""
    from backend.app.channels.bluebubbles import register_bluebubbles_webhook

    mock_resp = AsyncMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.settings.http_timeout_seconds", 10),
        patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await register_bluebubbles_webhook(
            "https://my-mac.example.com",
            "https://tunnel.example.com/api/webhooks/bluebubbles",
        )

    assert result is False


async def test_start_skips_without_config() -> None:
    """start() should be a no-op when server URL or password is empty."""
    channel = BlueBubblesChannel()
    with patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", ""):
        await channel.start()

    channel2 = BlueBubblesChannel()
    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://my-mac.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", ""),
    ):
        await channel2.start()


async def test_start_skips_without_tunnel() -> None:
    """start() should skip registration when no tunnel is detected."""
    channel = BlueBubblesChannel()
    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://my-mac.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.asyncio.sleep", new_callable=AsyncMock),
        patch.object(channel, "_check_server_reachable", new_callable=AsyncMock, return_value=True),
        patch(
            "backend.app.channels.bluebubbles.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "backend.app.channels.bluebubbles.register_bluebubbles_webhook",
            new_callable=AsyncMock,
        ) as mock_register,
    ):
        await channel.start()

    assert channel.server_reachable is True
    mock_register.assert_not_called()


async def test_start_skips_on_dns_failure() -> None:
    """start() should skip registration when DNS resolution fails."""
    channel = BlueBubblesChannel()
    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://my-mac.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.asyncio.sleep", new_callable=AsyncMock),
        patch.object(channel, "_check_server_reachable", new_callable=AsyncMock, return_value=True),
        patch(
            "backend.app.channels.bluebubbles.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.channels.bluebubbles.wait_for_dns",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "backend.app.channels.bluebubbles.register_bluebubbles_webhook",
            new_callable=AsyncMock,
        ) as mock_register,
    ):
        await channel.start()

    mock_register.assert_not_called()


async def test_start_registers_webhook_on_success() -> None:
    """start() should call register_bluebubbles_webhook when tunnel + DNS succeed."""
    channel = BlueBubblesChannel()
    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://my-mac.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.asyncio.sleep", new_callable=AsyncMock),
        patch.object(channel, "_check_server_reachable", new_callable=AsyncMock, return_value=True),
        patch(
            "backend.app.channels.bluebubbles.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.channels.bluebubbles.wait_for_dns",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.app.channels.bluebubbles.register_bluebubbles_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_register,
    ):
        await channel.start()

    assert channel.server_reachable is True
    mock_register.assert_called_once_with(
        "https://my-mac.example.com",
        "https://tunnel.example.com/api/webhooks/bluebubbles?password=test-pw",
    )


async def test_start_marks_unreachable_when_server_down() -> None:
    """start() should set server_reachable=False and skip registration when server is down."""
    channel = BlueBubblesChannel()
    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://unreachable.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.asyncio.sleep", new_callable=AsyncMock),
        patch.object(
            channel, "_check_server_reachable", new_callable=AsyncMock, return_value=False
        ),
        patch(
            "backend.app.channels.bluebubbles.register_bluebubbles_webhook",
            new_callable=AsyncMock,
        ) as mock_register,
    ):
        await channel.start()

    assert channel.server_reachable is False
    mock_register.assert_not_called()


async def test_check_server_reachable_success() -> None:
    """_check_server_reachable returns True when server responds."""
    channel = BlueBubblesChannel()

    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://my-mac.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = await channel._check_server_reachable()

    assert result is True


async def test_check_server_reachable_connect_error() -> None:
    """_check_server_reachable returns False on connection failure."""
    channel = BlueBubblesChannel()

    with (
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
            "https://unreachable.example.com",
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "test-pw"),
        patch("backend.app.channels.bluebubbles.httpx.AsyncClient") as mock_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("Name or service not known")
        mock_cls.return_value = mock_client

        result = await channel._check_server_reachable()

    assert result is False
