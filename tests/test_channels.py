"""Tests for channel base class, ChannelManager, and protocol conformance."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter

from backend.app.bus import message_bus
from backend.app.channels.base import BaseChannel
from backend.app.channels.manager import ChannelManager
from backend.app.media.download import DownloadedMedia

# -- Stub channel for manager tests ----------------------------------------


class _StubChannel(BaseChannel):
    """Minimal concrete channel for testing ChannelManager."""

    def __init__(self, channel_name: str) -> None:
        self._name = channel_name
        self.started = False
        self.stopped = False
        self.stopped_typing_for: list[str] = []

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

    async def stop_typing_indicator(self, to: str) -> None:
        self.stopped_typing_for.append(to)

    async def download_media(self, file_id: str) -> DownloadedMedia:
        return DownloadedMedia(
            content=b"", mime_type="application/octet-stream", original_url="", filename="stub"
        )


# -- BaseChannel tests -----------------------------------------------------


def test_base_channel_cannot_be_instantiated() -> None:
    """BaseChannel is abstract and should not be directly instantiable."""
    with pytest.raises(TypeError):
        BaseChannel()


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


@pytest.mark.asyncio
async def test_handle_inbound_sends_error_fallback_on_crash() -> None:
    """When process_inbound_from_bus crashes, an error reply should be sent."""
    from backend.app.agent.ingestion import InboundMessage

    mgr = ChannelManager()
    ch = _StubChannel("bluebubbles")
    mgr.register(ch)

    inbound = InboundMessage(
        channel="bluebubbles",
        sender_id="+15551234567",
        text="hello",
    )

    with patch(
        "backend.app.agent.ingestion.process_inbound_from_bus",
        new_callable=AsyncMock,
        side_effect=RuntimeError("total crash"),
    ):
        await mgr._handle_inbound(inbound)

    # An error fallback message should have been published to the bus
    found = False
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator and outbound.chat_id == "+15551234567":
            assert "something went wrong" in outbound.content.lower()
            found = True
            break
    assert found, "Expected an error fallback message on the outbound bus"


@pytest.mark.asyncio
async def test_dispatcher_routes_typing_stop_to_channel() -> None:
    """An outbound with is_typing_stop=True must call channel.stop_typing_indicator,
    not send_text or send_typing_indicator."""
    from backend.app.bus import OutboundMessage

    mgr = ChannelManager()
    ch = _StubChannel("bluebubbles")
    mgr.register(ch)

    # Resolve a future on first stop_typing_indicator call so the test can
    # await dispatcher work directly instead of polling.
    called = asyncio.get_running_loop().create_future()
    original_stop = ch.stop_typing_indicator

    async def signaling_stop(to: str) -> None:
        await original_stop(to)
        if not called.done():
            called.set_result(to)

    ch.stop_typing_indicator = signaling_stop  # type: ignore[method-assign]

    # Drain anything already queued.
    while not message_bus.outbound.empty():
        message_bus.outbound.get_nowait()

    await message_bus.publish_outbound(
        OutboundMessage(
            channel="bluebubbles",
            chat_id="+15551234567",
            content="",
            is_typing_stop=True,
        )
    )

    task = asyncio.create_task(mgr._run_outbound_dispatcher())
    try:
        recipient = await asyncio.wait_for(called, timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert recipient == "+15551234567"
    assert ch.stopped_typing_for == ["+15551234567"]
