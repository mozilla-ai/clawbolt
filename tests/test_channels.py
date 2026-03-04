"""Tests for channel base class, ChannelManager, and protocol conformance."""

import asyncio

import pytest
from fastapi import APIRouter

from backend.app.channels.base import BaseChannel
from backend.app.channels.manager import ChannelManager
from backend.app.channels.telegram import TelegramChannel
from backend.app.media.download import DownloadedMedia
from backend.app.services.messaging import MessagingService

# -- Stub channel for manager tests ----------------------------------------


class _StubChannel(BaseChannel):
    """Minimal concrete channel for testing ChannelManager."""

    def __init__(self, channel_name: str) -> None:
        self._name = channel_name
        self.started = False
        self.stopped = False

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def get_router(self) -> APIRouter:
        return APIRouter()

    def is_allowed(self, sender_id: str, username: str) -> bool:
        return True

    async def send_text(self, to: str, body: str) -> str:
        return "stub-id"

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        return "stub-id"

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        return "stub-id"

    async def send_typing_indicator(self, to: str) -> None:
        pass

    async def download_media(self, file_id: str) -> DownloadedMedia:
        return DownloadedMedia(
            content=b"", mime_type="application/octet-stream", original_url="", filename="stub"
        )


# -- BaseChannel tests -----------------------------------------------------


def test_base_channel_cannot_be_instantiated() -> None:
    """BaseChannel is abstract and should not be directly instantiable."""
    with pytest.raises(TypeError):
        BaseChannel()


def test_telegram_channel_satisfies_messaging_protocol() -> None:
    """TelegramChannel should satisfy the MessagingService protocol."""
    channel = TelegramChannel(bot_token="test-token")
    assert isinstance(channel, MessagingService)


# -- ChannelManager tests --------------------------------------------------


def test_manager_register_and_get() -> None:
    """ChannelManager.register stores and get retrieves channels by name."""
    mgr = ChannelManager()
    ch = _StubChannel("sms")
    mgr.register(ch)
    assert mgr.get("sms") is ch


def test_manager_register_duplicate_raises() -> None:
    """Registering two channels with the same name raises ValueError."""
    mgr = ChannelManager()
    mgr.register(_StubChannel("telegram"))
    with pytest.raises(ValueError, match="already registered"):
        mgr.register(_StubChannel("telegram"))


def test_manager_get_unknown_raises() -> None:
    """Getting an unregistered channel name raises KeyError."""
    mgr = ChannelManager()
    with pytest.raises(KeyError):
        mgr.get("nonexistent")


def test_manager_get_default() -> None:
    """get_default returns the first registered channel."""
    mgr = ChannelManager()
    first = _StubChannel("telegram")
    mgr.register(first)
    mgr.register(_StubChannel("sms"))
    assert mgr.get_default() is first


def test_manager_get_default_empty_raises() -> None:
    """get_default raises RuntimeError when no channels are registered."""
    mgr = ChannelManager()
    with pytest.raises(RuntimeError, match="No channels registered"):
        mgr.get_default()


def test_manager_channels_returns_copy() -> None:
    """channels property returns a copy, not the internal dict."""
    mgr = ChannelManager()
    ch = _StubChannel("web")
    mgr.register(ch)
    channels = mgr.channels
    channels["injected"] = ch
    assert "injected" not in mgr.channels


@pytest.mark.asyncio
async def test_manager_start_all() -> None:
    """start_all calls start() on every registered channel."""
    mgr = ChannelManager()
    ch1 = _StubChannel("a")
    ch2 = _StubChannel("b")
    mgr.register(ch1)
    mgr.register(ch2)
    tasks = await mgr.start_all()
    # Wait for fire-and-forget tasks to finish
    await asyncio.gather(*tasks)
    assert ch1.started
    assert ch2.started


@pytest.mark.asyncio
async def test_manager_stop_all() -> None:
    """stop_all calls stop() on every registered channel."""
    mgr = ChannelManager()
    ch1 = _StubChannel("a")
    ch2 = _StubChannel("b")
    mgr.register(ch1)
    mgr.register(ch2)
    await mgr.stop_all()
    assert ch1.stopped
    assert ch2.stopped
