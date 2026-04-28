"""Tests for the unknown-sender reply helper and its integration with
``handle_webhook_inbound``."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter

from backend.app.agent.ingestion import InboundMessage
from backend.app.channels.base import BaseChannel, handle_webhook_inbound
from backend.app.channels.unknown_sender import (
    _build_reply_body,
    _claim_reply_slot,
    reply_to_unknown_sender,
    reset_unknown_sender_cache,
)
from backend.app.media.download import DownloadedMedia


class _RecordingChannel(BaseChannel):
    """Minimal channel that records send_text calls for assertions."""

    def __init__(self, name: str = "test", *, raise_on_send: bool = False) -> None:
        self._name = name
        self.sent: list[tuple[str, str]] = []
        self._raise_on_send = raise_on_send

    @property
    def name(self) -> str:
        return self._name

    def get_router(self) -> APIRouter:
        return APIRouter()

    def is_allowed(self, sender_id: str, username: str) -> bool:
        return False

    async def send_text(self, to: str, body: str) -> str:
        if self._raise_on_send:
            raise RuntimeError("simulated provider failure")
        self.sent.append((to, body))
        return "msg-id"

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        return "msg-id"

    async def send_typing_indicator(self, to: str) -> None:
        return None

    async def download_media(self, file_id: str) -> DownloadedMedia:
        return DownloadedMedia(
            content=b"", mime_type="application/octet-stream", original_url="", filename=""
        )


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_unknown_sender_cache()


# -- reply_to_unknown_sender direct tests ---------------------------------


@pytest.mark.asyncio
async def test_replies_to_unknown_sender() -> None:
    channel = _RecordingChannel()
    sent = await reply_to_unknown_sender(channel, "+15551234567")
    assert sent is True
    assert len(channel.sent) == 1
    to, body = channel.sent[0]
    assert to == "+15551234567"
    assert "Clawbolt" in body


@pytest.mark.asyncio
async def test_rate_limited_within_cooldown() -> None:
    channel = _RecordingChannel()
    with patch(
        "backend.app.channels.unknown_sender.settings.unknown_sender_reply_cooldown_seconds",
        86_400,
    ):
        first = await reply_to_unknown_sender(channel, "+15551234567")
        second = await reply_to_unknown_sender(channel, "+15551234567")
    assert first is True
    assert second is False
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_zero_cooldown_lets_every_message_reply() -> None:
    channel = _RecordingChannel()
    with patch(
        "backend.app.channels.unknown_sender.settings.unknown_sender_reply_cooldown_seconds", 0
    ):
        await reply_to_unknown_sender(channel, "+15551234567")
        await reply_to_unknown_sender(channel, "+15551234567")
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_per_sender_isolation() -> None:
    channel = _RecordingChannel()
    await reply_to_unknown_sender(channel, "+15551111111")
    await reply_to_unknown_sender(channel, "+15552222222")
    assert {to for to, _ in channel.sent} == {"+15551111111", "+15552222222"}


@pytest.mark.asyncio
async def test_per_channel_isolation() -> None:
    """The cooldown is keyed by (channel, sender), so the same number on a
    different channel still gets a reply."""
    sms = _RecordingChannel("sms")
    imessage = _RecordingChannel("imessage")
    await reply_to_unknown_sender(sms, "+15551234567")
    await reply_to_unknown_sender(imessage, "+15551234567")
    assert len(sms.sent) == 1
    assert len(imessage.sent) == 1


@pytest.mark.asyncio
async def test_send_failure_is_swallowed_but_consumes_slot() -> None:
    """A failed send still updates the cooldown so a flood of inbound from one
    spoofed sender can't trigger repeated outbound attempts."""
    channel = _RecordingChannel(raise_on_send=True)
    first = await reply_to_unknown_sender(channel, "+15551234567")
    second = await reply_to_unknown_sender(channel, "+15551234567")
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_empty_sender_id_is_skipped() -> None:
    channel = _RecordingChannel()
    sent = await reply_to_unknown_sender(channel, "")
    assert sent is False
    assert channel.sent == []


def test_reply_body_includes_signup_url_when_set() -> None:
    with patch(
        "backend.app.channels.unknown_sender.settings.unknown_sender_signup_url",
        "https://app.clawbolt.ai/signup",
    ):
        body = _build_reply_body()
    assert "https://app.clawbolt.ai/signup" in body


def test_reply_body_falls_back_when_url_unset() -> None:
    with patch("backend.app.channels.unknown_sender.settings.unknown_sender_signup_url", ""):
        body = _build_reply_body()
    assert "clawbolt.ai" in body


def test_claim_reply_slot_is_monotonic_per_now() -> None:
    """``_claim_reply_slot`` accepts an explicit *now* so we can assert
    cooldown boundaries deterministically without sleeping."""
    with patch(
        "backend.app.channels.unknown_sender.settings.unknown_sender_reply_cooldown_seconds",
        60,
    ):
        assert _claim_reply_slot("ch", "s", now=100.0) is True
        assert _claim_reply_slot("ch", "s", now=159.0) is False
        assert _claim_reply_slot("ch", "s", now=160.5) is True


# -- handle_webhook_inbound integration tests -----------------------------


@pytest.mark.asyncio
async def test_handle_webhook_inbound_replies_when_allowlist_rejects(
    _stub_unknown_sender_reply: AsyncMock,
) -> None:
    """When ``is_allowed`` returns False, ``handle_webhook_inbound`` must still
    return 200 and trigger a reply attempt to the unknown sender."""
    channel = _RecordingChannel("sms")
    inbound = InboundMessage(
        channel="sms",
        sender_id="+15551234567",
        sender_username=None,
        text="Hello",
        media_refs=[],
        external_message_id="ext-1",
    )

    resp = await handle_webhook_inbound(channel, inbound)

    assert resp.status_code == 200
    _stub_unknown_sender_reply.assert_awaited_once_with(channel, "+15551234567")
