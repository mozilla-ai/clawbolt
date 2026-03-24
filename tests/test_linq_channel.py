"""Tests for Linq channel: webhook handling and outbound messaging.

Mirrors the structure of test_telegram_webhook.py. The webhook handler
validates HMAC signatures, parses payloads, checks the allowlist, and
publishes InboundMessages to the bus.
"""

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app.channels.linq import LinqChannel
from tests.mocks.linq import (
    LINQ_TEST_SIGNING_SECRET,
    make_linq_webhook_headers,
    make_linq_webhook_payload,
)

_PATCH_BUS_PUBLISH = "backend.app.channels.linq.message_bus.publish_inbound"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_webhook(
    client: TestClient,
    payload: dict,
    signing_secret: str = "",
    timestamp: int | None = None,
) -> httpx.Response:
    """Post a Linq webhook with optional HMAC headers."""
    body = json.dumps(payload).encode()
    if signing_secret:
        headers = make_linq_webhook_headers(body, signing_secret, timestamp)
    else:
        headers = {"Content-Type": "application/json"}
    return client.post("/api/webhooks/linq", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Webhook endpoint tests
# ---------------------------------------------------------------------------


def test_inbound_webhook_returns_200(linq_client: TestClient) -> None:
    """Valid webhook payload should return 200 with ok:true."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        payload = make_linq_webhook_payload(text="Hello")
        resp = _post_webhook(linq_client, payload)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_inbound_webhook_publishes_text(linq_client: TestClient) -> None:
    """Inbound text message should be published to the bus."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_linq_webhook_payload(sender="+15559876543", text="Need a quote")
        _post_webhook(linq_client, payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "linq"
    assert inbound.sender_id == "+15559876543"
    assert inbound.text == "Need a quote"


def test_inbound_webhook_publishes_media(linq_client: TestClient) -> None:
    """Media messages should include media_refs in the published InboundMessage."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_linq_webhook_payload(
            text="Check this out",
            media_url="https://cdn.linqapp.com/media/photo.jpg",
        )
        _post_webhook(linq_client, payload)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Check this out"
    assert len(inbound.media_refs) == 1
    assert inbound.media_refs[0][0] == "https://cdn.linqapp.com/media/photo.jpg"
    assert inbound.media_refs[0][1] == "image/jpeg"


def test_invalid_hmac_does_not_publish(linq_client: TestClient) -> None:
    """Invalid HMAC signature should return 200 but not publish to bus."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.linq.settings.linq_webhook_signing_secret",
            LINQ_TEST_SIGNING_SECRET,
        ),
    ):
        payload = make_linq_webhook_payload(text="Hi")
        body = json.dumps(payload).encode()
        headers = {
            "X-Webhook-Signature": "invalid-signature",
            "X-Webhook-Timestamp": str(int(time.time())),
            "Content-Type": "application/json",
        }
        resp = linq_client.post("/api/webhooks/linq", content=body, headers=headers)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_stale_timestamp_does_not_publish(linq_client: TestClient) -> None:
    """Stale timestamp (replay attack) should return 200 but not publish."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.linq.settings.linq_webhook_signing_secret",
            LINQ_TEST_SIGNING_SECRET,
        ),
    ):
        payload = make_linq_webhook_payload(text="Hi")
        stale_ts = int(time.time()) - 600  # 10 minutes ago
        resp = _post_webhook(
            linq_client, payload, signing_secret=LINQ_TEST_SIGNING_SECRET, timestamp=stale_ts
        )

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_valid_hmac_publishes(linq_client: TestClient) -> None:
    """Valid HMAC signature should publish to bus."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.linq.settings.linq_webhook_signing_secret",
            LINQ_TEST_SIGNING_SECRET,
        ),
    ):
        payload = make_linq_webhook_payload(text="Hi")
        resp = _post_webhook(linq_client, payload, signing_secret=LINQ_TEST_SIGNING_SECRET)

    assert resp.status_code == 200
    mock_pub.assert_called_once()


def test_duplicate_message_skipped(linq_client: TestClient) -> None:
    """Duplicate webhook calls should not publish to bus twice."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_linq_webhook_payload(text="First", message_id="dup-msg-001")
        _post_webhook(linq_client, payload)
        _post_webhook(linq_client, payload)

    mock_pub.assert_called_once()


def test_non_message_received_event_ignored(linq_client: TestClient) -> None:
    """Non-message.received events should return 200 without publishing."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        for event in ("message.delivered", "message.read", "message.failed"):
            payload = make_linq_webhook_payload(text="Hi", event=event)
            resp = _post_webhook(linq_client, payload)
            assert resp.status_code == 200

    mock_pub.assert_not_called()


def test_invalid_json_returns_200(linq_client: TestClient) -> None:
    """Invalid JSON body should return 200 without crashing."""
    resp = linq_client.post(
        "/api/webhooks/linq",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_outbound_direction_ignored(linq_client: TestClient) -> None:
    """Outbound messages should be ignored."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        payload = make_linq_webhook_payload(text="Echo", direction="outbound")
        resp = _post_webhook(linq_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Allowlist tests
# ---------------------------------------------------------------------------


def test_allowlist_empty_denies_all(linq_client: TestClient) -> None:
    """Empty allowlist should deny all phone numbers."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", ""),
    ):
        payload = make_linq_webhook_payload(sender="+15551234567", text="Hi")
        resp = _post_webhook(linq_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


def test_allowlist_wildcard_allows_all(linq_client: TestClient) -> None:
    """Setting allowed_numbers to '*' should allow all phone numbers."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", "*"),
    ):
        payload = make_linq_webhook_payload(sender="+15559999999", text="Hi")
        resp = _post_webhook(linq_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_called_once()


def test_allowlist_matching_number_allows(linq_client: TestClient) -> None:
    """Matching E.164 number should be allowed."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", "+15551234567"),
    ):
        payload = make_linq_webhook_payload(sender="+15551234567", text="Hi")
        resp = _post_webhook(linq_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_called_once()


def test_allowlist_non_matching_number_denies(linq_client: TestClient) -> None:
    """Non-matching phone number should be denied."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.linq.settings.linq_allowed_numbers", "+15551234567"),
    ):
        payload = make_linq_webhook_payload(sender="+15559999999", text="Hi")
        resp = _post_webhook(linq_client, payload)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# is_allowed() unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_is_allowed_empty_denies() -> None:
    channel = LinqChannel()
    with patch("backend.app.channels.linq.settings.linq_allowed_numbers", ""):
        assert channel.is_allowed("+15551234567", "") is False


def test_is_allowed_wildcard_allows() -> None:
    channel = LinqChannel()
    with patch("backend.app.channels.linq.settings.linq_allowed_numbers", "*"):
        assert channel.is_allowed("+15551234567", "") is True


def test_is_allowed_matching_number() -> None:
    channel = LinqChannel()
    with patch("backend.app.channels.linq.settings.linq_allowed_numbers", "+15551234567"):
        assert channel.is_allowed("+15551234567", "") is True
        assert channel.is_allowed("+15559999999", "") is False


# ---------------------------------------------------------------------------
# Outbound tests (mock httpx)
# ---------------------------------------------------------------------------


def _make_mock_http(json_response: dict | None = None) -> AsyncMock:
    """Create a mock httpx.AsyncClient with common setup."""
    mock = AsyncMock()
    if json_response is not None:
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json = lambda: json_response
        mock_resp.content = b""
        mock_resp.headers = {}
        mock.post.return_value = mock_resp
        mock.get.return_value = mock_resp
    return mock


async def test_send_text_with_cached_chat_id() -> None:
    """send_text should use cached chat_id for existing conversations."""
    channel = LinqChannel()
    channel._chat_cache["+15551234567"] = "cached-chat-uuid"

    mock_http = _make_mock_http({"message_id": "resp-msg-001", "chat_id": "cached-chat-uuid"})
    channel._client = mock_http

    result = await channel.send_text("+15551234567", "Hello there")

    assert result == "resp-msg-001"
    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "/chats/cached-chat-uuid/messages"


async def test_send_text_without_cached_chat_id() -> None:
    """send_text should create a new chat when no cached chat_id exists."""
    channel = LinqChannel()

    mock_http = _make_mock_http({"id": "new-chat-uuid", "message_id": "new-msg-001"})
    channel._client = mock_http

    await channel.send_text("+15559876543", "Hello")

    mock_http.post.assert_called_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "/chats"
    # Should now be cached
    assert channel._chat_cache["+15559876543"] == "new-chat-uuid"


async def test_send_typing_indicator_with_cached_chat() -> None:
    """send_typing_indicator should POST to the typing endpoint."""
    channel = LinqChannel()
    channel._chat_cache["+15551234567"] = "chat-uuid"

    mock_http = _make_mock_http({})
    channel._client = mock_http

    await channel.send_typing_indicator("+15551234567")

    mock_http.post.assert_called_once_with("/chats/chat-uuid/typing")


async def test_send_typing_indicator_without_cached_chat() -> None:
    """send_typing_indicator should be a no-op without a cached chat_id."""
    channel = LinqChannel()
    mock_http = _make_mock_http({})
    channel._client = mock_http

    await channel.send_typing_indicator("+15551234567")

    mock_http.post.assert_not_called()


async def test_download_media_from_cdn() -> None:
    """download_media should fetch content from the CDN URL."""
    channel = LinqChannel()

    mock_http = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.content = b"fake-image-data"
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.raise_for_status = lambda: None
    mock_http.get.return_value = mock_resp
    channel._client = mock_http

    result = await channel.download_media("https://cdn.linqapp.com/media/photo.jpg")

    assert result.content == b"fake-image-data"
    assert result.mime_type == "image/jpeg"
    assert result.original_url == "https://cdn.linqapp.com/media/photo.jpg"


# ---------------------------------------------------------------------------
# Chat cache population test
# ---------------------------------------------------------------------------


def test_webhook_populates_chat_cache(linq_client: TestClient) -> None:
    """Inbound webhook should populate the chat cache for outbound use."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        payload = make_linq_webhook_payload(
            sender="+15551234567",
            text="Hello",
            chat_id="webhook-chat-uuid",
        )
        _post_webhook(linq_client, payload)

    # Get the LinqChannel instance from the app
    from backend.app.channels import get_channel

    channel = get_channel("linq")
    assert isinstance(channel, LinqChannel)
    assert channel._chat_cache.get("+15551234567") == "webhook-chat-uuid"


# ---------------------------------------------------------------------------
# Webhook auto-registration tests
# ---------------------------------------------------------------------------


async def test_register_linq_webhook_success() -> None:
    """register_linq_webhook should POST to webhook-subscriptions and return True."""
    from backend.app.channels.linq import register_linq_webhook

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"id": "sub-001", "signing_secret": "new-secret"}
    mock_resp.text = ""

    with (
        patch("backend.app.channels.linq.settings.linq_api_token", "test-token"),
        patch("backend.app.channels.linq.settings.linq_webhook_signing_secret", ""),
        patch("backend.app.channels.linq.settings.http_timeout_seconds", 10),
        patch("backend.app.channels.linq.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await register_linq_webhook("https://tunnel.example.com/api/webhooks/linq")

    assert result is True
    mock_client.post.assert_called_once()


async def test_register_linq_webhook_failure() -> None:
    """register_linq_webhook should return False on HTTP error."""
    from backend.app.channels.linq import register_linq_webhook

    mock_resp = AsyncMock()
    mock_resp.status_code = 422
    mock_resp.text = "Unprocessable Entity"

    with (
        patch("backend.app.channels.linq.settings.linq_api_token", "test-token"),
        patch("backend.app.channels.linq.settings.http_timeout_seconds", 10),
        patch("backend.app.channels.linq.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await register_linq_webhook("https://tunnel.example.com/api/webhooks/linq")

    assert result is False


async def test_start_skips_without_token() -> None:
    """start() should be a no-op when linq_api_token is empty."""
    channel = LinqChannel()
    with patch("backend.app.channels.linq.settings.linq_api_token", ""):
        await channel.start()
    # No exception, no calls -- just returns


async def test_start_skips_without_tunnel() -> None:
    """start() should skip registration when no tunnel is detected."""
    channel = LinqChannel()
    with (
        patch("backend.app.channels.linq.settings.linq_api_token", "test-token"),
        patch("backend.app.channels.linq.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "backend.app.channels.linq.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "backend.app.channels.linq.register_linq_webhook",
            new_callable=AsyncMock,
        ) as mock_register,
    ):
        await channel.start()

    mock_register.assert_not_called()


async def test_start_skips_on_dns_failure() -> None:
    """start() should skip registration when DNS resolution fails."""
    channel = LinqChannel()
    with (
        patch("backend.app.channels.linq.settings.linq_api_token", "test-token"),
        patch("backend.app.channels.linq.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "backend.app.channels.linq.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.channels.linq.wait_for_dns",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "backend.app.channels.linq.register_linq_webhook",
            new_callable=AsyncMock,
        ) as mock_register,
    ):
        await channel.start()

    mock_register.assert_not_called()


async def test_start_registers_webhook_on_success() -> None:
    """start() should call register_linq_webhook when tunnel + DNS succeed."""
    channel = LinqChannel()
    with (
        patch("backend.app.channels.linq.settings.linq_api_token", "test-token"),
        patch("backend.app.channels.linq.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "backend.app.channels.linq.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.channels.linq.wait_for_dns",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.app.channels.linq.register_linq_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_register,
    ):
        await channel.start()

    mock_register.assert_called_once_with("https://tunnel.example.com/api/webhooks/linq")
