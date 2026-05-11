"""Tests for the Twilio channel: webhook handling, signature validation,
parsing, allowlist gating, idempotency, and outbound SMS/MMS sending.

Mirrors the structure of ``test_linq_channel.py``. The Twilio SDK is mocked
at its REST entry point so no live Twilio account is required.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.channels.twilio import TWIML_EMPTY, TwilioChannel
from tests.mocks.twilio import TWILIO_TEST_AUTH_TOKEN, make_twilio_form, sign_twilio_form

_PATCH_BUS_PUBLISH = "backend.app.bus.message_bus.publish_inbound"
_WEBHOOK_URL = "http://testserver/api/webhooks/twilio"


async def _post_form(
    client: httpx.AsyncClient,
    form: dict[str, str],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a Twilio-style form payload to the webhook endpoint."""
    return await client.post(
        "/api/webhooks/twilio",
        data=form,
        headers=headers or {"Content-Type": "application/x-www-form-urlencoded"},
    )


# ---------------------------------------------------------------------------
# Webhook endpoint tests
# ---------------------------------------------------------------------------


async def test_inbound_webhook_returns_empty_twiml(twilio_client: httpx.AsyncClient) -> None:
    """Valid webhook payload returns 200 with empty TwiML."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock):
        resp = await _post_form(twilio_client, make_twilio_form(text="Hello"))
    assert resp.status_code == 200
    assert resp.text == TWIML_EMPTY
    assert resp.headers["content-type"].startswith("application/xml")


async def test_inbound_webhook_publishes_text(twilio_client: httpx.AsyncClient) -> None:
    """Inbound text message is published to the bus."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        form = make_twilio_form(sender="+15559876543", text="Need a quote")
        await _post_form(twilio_client, form)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "twilio"
    assert inbound.sender_id == "+15559876543"
    assert inbound.text == "Need a quote"


async def test_inbound_webhook_publishes_media(twilio_client: httpx.AsyncClient) -> None:
    """MediaUrl/MediaContentType fields are surfaced in the published InboundMessage."""
    media = [
        ("https://api.twilio.com/Accounts/AC0/Messages/MM0/Media/ME0", "image/jpeg"),
        ("https://api.twilio.com/Accounts/AC0/Messages/MM0/Media/ME1", "image/png"),
    ]
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        form = make_twilio_form(text="Pics", media=media)
        await _post_form(twilio_client, form)

    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "Pics"
    assert inbound.media_refs == media


async def test_inbound_webhook_uses_message_sid_for_idempotency(
    twilio_client: httpx.AsyncClient,
) -> None:
    """external_message_id is keyed off MessageSid so duplicates are dropped."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        form = make_twilio_form(message_sid="SM1234567890")
        await _post_form(twilio_client, form)
        # Send the same MessageSid again to trigger dedup
        await _post_form(twilio_client, form)

    # Only the first delivery should publish; the duplicate is blocked at
    # the idempotency store inside handle_webhook_inbound.
    assert mock_pub.call_count == 1
    inbound = mock_pub.call_args[0][0]
    assert inbound.external_message_id == "twilio_SM1234567890"


async def test_inbound_webhook_blocked_by_allowlist(twilio_client: httpx.AsyncClient) -> None:
    """A sender not on the allowlist is rejected before bus publish."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch(
            "backend.app.channels.twilio.settings.twilio_allowed_numbers",
            "+15550000000",
        ),
    ):
        form = make_twilio_form(sender="+15551111111")
        resp = await _post_form(twilio_client, form)

    assert resp.status_code == 200
    mock_pub.assert_not_called()


async def test_inbound_webhook_missing_from_returns_empty(
    twilio_client: httpx.AsyncClient,
) -> None:
    """A payload without From is ignored gracefully (no crash, no publish)."""
    with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
        form = make_twilio_form()
        del form["From"]
        resp = await _post_form(twilio_client, form)

    assert resp.status_code == 200
    assert resp.text == TWIML_EMPTY
    mock_pub.assert_not_called()


async def test_inbound_webhook_invalid_signature_dropped(
    twilio_client: httpx.AsyncClient,
) -> None:
    """When signature validation is on and the header is wrong, the bus is not called."""
    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.twilio.settings.twilio_validate_signatures", True),
        patch(
            "backend.app.channels.twilio.settings.twilio_auth_token",
            TWILIO_TEST_AUTH_TOKEN,
        ),
    ):
        form = make_twilio_form()
        resp = await _post_form(
            twilio_client,
            form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": "obviously-wrong-signature",
            },
        )

    assert resp.status_code == 200
    assert resp.text == TWIML_EMPTY
    mock_pub.assert_not_called()


async def test_inbound_webhook_valid_signature_accepted(
    twilio_client: httpx.AsyncClient,
) -> None:
    """A correctly signed request passes the signature gate and reaches the bus."""
    form = make_twilio_form(text="signed")
    signature = sign_twilio_form(_WEBHOOK_URL, form)

    with (
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
        patch("backend.app.channels.twilio.settings.twilio_validate_signatures", True),
        patch(
            "backend.app.channels.twilio.settings.twilio_auth_token",
            TWILIO_TEST_AUTH_TOKEN,
        ),
    ):
        resp = await _post_form(
            twilio_client,
            form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature,
            },
        )

    assert resp.status_code == 200
    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.text == "signed"


# ---------------------------------------------------------------------------
# Outbound tests (mock the Twilio REST SDK at the boundary)
# ---------------------------------------------------------------------------


def _make_channel_with_mocked_client() -> tuple[TwilioChannel, MagicMock]:
    """Build a channel whose ``client.messages.create`` returns a stub message."""
    channel = TwilioChannel()
    mock_message = MagicMock()
    mock_message.sid = "SMabcdef"
    mock_client = MagicMock()
    mock_client.messages.create = MagicMock(return_value=mock_message)
    channel._client = mock_client
    return channel, mock_client


async def test_send_text_uses_phone_number_when_no_messaging_service() -> None:
    """With only ``twilio_phone_number`` set, ``from_`` is pinned."""
    channel, mock_client = _make_channel_with_mocked_client()

    with (
        patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"),
        patch("backend.app.channels.twilio.settings.twilio_messaging_service_sid", ""),
    ):
        sid = await channel.send_text("+15551234567", "hi")

    assert sid == "SMabcdef"
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs == {
        "to": "+15551234567",
        "body": "hi",
        "from_": "+15550000001",
    }


async def test_send_text_prefers_messaging_service_sid() -> None:
    """When both are set, the Messaging Service SID wins (A2P 10DLC path)."""
    channel, mock_client = _make_channel_with_mocked_client()

    with (
        patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"),
        patch(
            "backend.app.channels.twilio.settings.twilio_messaging_service_sid",
            "MG" + "0" * 32,
        ),
    ):
        await channel.send_text("+15551234567", "hi")

    kwargs = mock_client.messages.create.call_args.kwargs
    assert "from_" not in kwargs
    assert kwargs["messaging_service_sid"] == "MG" + "0" * 32


async def test_send_text_raises_when_no_sender_configured() -> None:
    """Refuse to send when neither sender is configured: silent drops would hide misconfig."""
    channel, _ = _make_channel_with_mocked_client()
    with (
        patch("backend.app.channels.twilio.settings.twilio_phone_number", ""),
        patch("backend.app.channels.twilio.settings.twilio_messaging_service_sid", ""),
        pytest.raises(RuntimeError, match="TWILIO_PHONE_NUMBER"),
    ):
        await channel.send_text("+15551234567", "hi")


async def test_send_media_attaches_single_media_url() -> None:
    """send_media wraps the URL in a one-element list for the SDK."""
    channel, mock_client = _make_channel_with_mocked_client()
    with patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"):
        await channel.send_media("+15551234567", "see attached", "https://example.com/img.jpg")

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["media_url"] == ["https://example.com/img.jpg"]
    assert kwargs["body"] == "see attached"


async def test_send_message_sends_one_mms_for_multiple_media() -> None:
    """Multi-URL outbound is one MMS, not N sends (overrides BaseChannel default)."""
    channel, mock_client = _make_channel_with_mocked_client()
    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    with patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"):
        await channel.send_message("+15551234567", "two pics", media_urls=urls)

    # Exactly one create() call, not two.
    assert mock_client.messages.create.call_count == 1
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["media_url"] == urls
    assert kwargs["body"] == "two pics"


async def test_per_user_from_resolver_overrides_global_sender() -> None:
    """When a from_resolver is registered, its return value pins from_ per recipient."""
    from backend.app.channels.twilio import set_twilio_from_resolver

    channel, mock_client = _make_channel_with_mocked_client()

    async def resolver(to: str) -> str | None:
        return "+18001111111" if to == "+15551234567" else None

    set_twilio_from_resolver(resolver)
    try:
        with patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"):
            await channel.send_text("+15551234567", "hi")
    finally:
        # Reset so other tests aren't poisoned by this one's resolver.
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._from_resolver = None

    kwargs = mock_client.messages.create.call_args.kwargs
    # The per-user resolver should win over the global TWILIO_PHONE_NUMBER.
    assert kwargs["from_"] == "+18001111111"


async def test_per_user_from_resolver_falls_back_when_none() -> None:
    """A resolver that returns None falls back to the global Twilio sender."""
    from backend.app.channels.twilio import set_twilio_from_resolver

    channel, mock_client = _make_channel_with_mocked_client()

    async def resolver(to: str) -> str | None:
        return None

    set_twilio_from_resolver(resolver)
    try:
        with patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"):
            await channel.send_text("+15551234567", "hi")
    finally:
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._from_resolver = None

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["from_"] == "+15550000001"


async def test_per_user_resolver_failure_falls_back_to_global() -> None:
    """A resolver that raises should not block the send; we fall back to global."""
    from backend.app.channels.twilio import set_twilio_from_resolver

    channel, mock_client = _make_channel_with_mocked_client()

    async def resolver(to: str) -> str | None:
        raise RuntimeError("db unreachable")

    set_twilio_from_resolver(resolver)
    try:
        with patch("backend.app.channels.twilio.settings.twilio_phone_number", "+15550000001"):
            sid = await channel.send_text("+15551234567", "hi")
    finally:
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._from_resolver = None

    assert sid == "SMabcdef"
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["from_"] == "+15550000001"


async def test_send_typing_indicator_is_noop() -> None:
    """send_typing_indicator must not raise and must not touch the SDK.

    SMS has no typing-indicator concept; the override exists only to
    satisfy BaseChannel.
    """
    channel, mock_client = _make_channel_with_mocked_client()
    await channel.send_typing_indicator("+15551234567")
    mock_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Media download tests
# ---------------------------------------------------------------------------


async def test_download_media_uses_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """download_media authenticates with the account SID + auth token."""
    captured_auth: list[httpx.BasicAuth] = []

    async def fake_download_bounded(
        client: httpx.AsyncClient, url: str
    ) -> tuple[bytes, httpx.Headers]:
        # Capture the auth attached to the client so the test can verify
        # we passed Basic Auth instead of a raw Bearer.
        captured_auth.append(client.auth)  # type: ignore[arg-type]
        return b"image-bytes", httpx.Headers({"content-type": "image/jpeg"})

    monkeypatch.setattr("backend.app.channels.twilio.download_bounded", fake_download_bounded)

    channel = TwilioChannel()
    channel._account_sid = "ACtest"
    channel._auth_token = "tkn"

    media = await channel.download_media(
        "https://api.twilio.com/Accounts/AC0/Messages/MM0/Media/ME0"
    )

    assert media.content == b"image-bytes"
    assert media.mime_type == "image/jpeg"
    assert media.filename.endswith(".jpg")
    assert isinstance(captured_auth[0], httpx.BasicAuth)


async def test_download_media_rejects_non_twilio_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """download_media refuses URLs that aren't on a Twilio-owned host.

    Defense in depth: even if signature validation is off and a malicious
    webhook injects ``MediaUrl0=http://internal/...``, we must not send
    the account-SID Basic Auth header to that host.
    """
    called = False

    async def fake_download_bounded(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return b"", httpx.Headers()

    monkeypatch.setattr("backend.app.channels.twilio.download_bounded", fake_download_bounded)

    channel = TwilioChannel()
    channel._account_sid = "ACtest"
    channel._auth_token = "tkn"

    with pytest.raises(ValueError, match="non-Twilio host"):
        await channel.download_media("http://attacker.example.com/leak")
    assert called is False


async def test_to_verifier_drops_inbound_when_pair_unknown(
    twilio_client: httpx.AsyncClient,
) -> None:
    """A registered To-verifier returning False causes the webhook to drop the message."""
    from backend.app.channels.twilio import set_twilio_to_verifier

    seen: list[tuple[str, str]] = []

    async def verifier(from_phone: str, to_phone: str) -> bool:
        seen.append((from_phone, to_phone))
        return False

    set_twilio_to_verifier(verifier)
    try:
        with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
            form = make_twilio_form(sender="+15555555555", to="+18001111111", text="hi")
            resp = await _post_form(twilio_client, form)
    finally:
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._to_verifier = None

    assert resp.status_code == 200
    assert resp.text == TWIML_EMPTY
    mock_pub.assert_not_called()
    assert seen == [("+15555555555", "+18001111111")]


async def test_to_verifier_accepts_inbound_when_pair_matches(
    twilio_client: httpx.AsyncClient,
) -> None:
    """A verifier returning True lets the message through to the bus."""
    from backend.app.channels.twilio import set_twilio_to_verifier

    async def verifier(from_phone: str, to_phone: str) -> bool:
        return True

    set_twilio_to_verifier(verifier)
    try:
        with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
            form = make_twilio_form(text="ok")
            await _post_form(twilio_client, form)
    finally:
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._to_verifier = None

    mock_pub.assert_called_once()


async def test_to_verifier_exception_drops_inbound(
    twilio_client: httpx.AsyncClient,
) -> None:
    """A verifier that raises should drop the message, not 500.

    Choosing drop-on-error rather than fall-back-to-allow because the
    verifier is the *security* hook for premium; the from-resolver is
    the *outbound* hook where failure can safely degrade.
    """
    from backend.app.channels.twilio import set_twilio_to_verifier

    async def verifier(from_phone: str, to_phone: str) -> bool:
        raise RuntimeError("db unreachable")

    set_twilio_to_verifier(verifier)
    try:
        with patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub:
            form = make_twilio_form()
            resp = await _post_form(twilio_client, form)
    finally:
        import backend.app.channels.twilio as twilio_mod

        twilio_mod._to_verifier = None

    assert resp.status_code == 200
    mock_pub.assert_not_called()


async def test_parse_form_warns_on_missing_message_sid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing MessageSid is real-world impossible from Twilio; log loud to surface upstream issues."""
    form = {"From": "+15551234567", "Body": "test", "NumMedia": "0"}
    caplog.set_level(logging.WARNING, logger="backend.app.channels.twilio")
    inbound = TwilioChannel.parse_form(form)
    assert inbound is not None
    assert inbound.external_message_id == ""
    assert any("missing MessageSid" in rec.message for rec in caplog.records)
