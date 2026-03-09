"""Tests for the async message bus."""

import asyncio

import pytest

from backend.app.agent.ingestion import InboundMessage
from backend.app.bus import MessageBus, OutboundMessage


@pytest.mark.asyncio
async def test_publish_consume_inbound_round_trip() -> None:
    """Inbound messages should survive a publish/consume round-trip."""
    bus = MessageBus()
    msg = InboundMessage(channel="telegram", sender_id="123", text="hello")
    await bus.publish_inbound(msg)
    result = await bus.consume_inbound()
    assert result is msg
    assert result.channel == "telegram"
    assert result.text == "hello"


@pytest.mark.asyncio
async def test_publish_consume_outbound_round_trip() -> None:
    """Outbound messages should survive a publish/consume round-trip."""
    bus = MessageBus()
    msg = OutboundMessage(channel="telegram", chat_id="456", content="reply")
    await bus.publish_outbound(msg)
    result = await bus.consume_outbound()
    assert result is msg
    assert result.content == "reply"


@pytest.mark.asyncio
async def test_response_future_registration_and_resolution() -> None:
    """Registering and resolving a response future should deliver the outbound message."""
    bus = MessageBus()
    request_id = "req-001"
    fut = bus.register_response_future(request_id)

    msg = OutboundMessage(channel="webchat", chat_id="1", content="done", request_id=request_id)
    resolved = bus.resolve_response(request_id, msg)

    assert resolved is True
    assert fut.done()
    assert fut.result() is msg


@pytest.mark.asyncio
async def test_resolve_unknown_request_id_returns_false() -> None:
    """Resolving an unregistered request_id should return False."""
    bus = MessageBus()
    msg = OutboundMessage(channel="webchat", chat_id="1", content="x", request_id="unknown")
    assert bus.resolve_response("unknown", msg) is False


@pytest.mark.asyncio
async def test_wait_for_response_resolves() -> None:
    """wait_for_response should return when the response future is resolved."""
    bus = MessageBus()
    request_id = "req-002"
    bus.register_response_future(request_id)

    msg = OutboundMessage(channel="webchat", chat_id="1", content="hi", request_id=request_id)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        bus.resolve_response(request_id, msg)

    task = asyncio.create_task(resolve_later())
    result = await bus.wait_for_response(request_id, timeout=5)
    assert result is msg
    await task


@pytest.mark.asyncio
async def test_wait_for_response_timeout() -> None:
    """wait_for_response should raise TimeoutError when no reply arrives."""
    bus = MessageBus()
    bus.register_response_future("req-timeout")
    with pytest.raises(asyncio.TimeoutError):
        await bus.wait_for_response("req-timeout", timeout=0.05)


@pytest.mark.asyncio
async def test_wait_for_response_auto_registers_future() -> None:
    """wait_for_response should auto-register a future if one does not exist."""
    bus = MessageBus()
    request_id = "req-auto"
    msg = OutboundMessage(channel="webchat", chat_id="1", content="auto", request_id=request_id)

    async def resolve_later() -> None:
        await asyncio.sleep(0.05)
        bus.resolve_response(request_id, msg)

    task = asyncio.create_task(resolve_later())
    result = await bus.wait_for_response(request_id, timeout=5)
    assert result.content == "auto"
    await task


@pytest.mark.asyncio
async def test_queue_sizes() -> None:
    """Queue size properties should reflect pending messages."""
    bus = MessageBus()
    assert bus.inbound_size == 0
    assert bus.outbound_size == 0

    await bus.publish_inbound(InboundMessage(channel="t", sender_id="1", text="a"))
    assert bus.inbound_size == 1

    await bus.publish_outbound(OutboundMessage(channel="t", chat_id="1", content="b"))
    assert bus.outbound_size == 1

    await bus.consume_inbound()
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_resolve_cleans_up_future() -> None:
    """After resolution, the future should be removed from the internal map."""
    bus = MessageBus()
    request_id = "req-cleanup"
    bus.register_response_future(request_id)
    msg = OutboundMessage(channel="webchat", chat_id="1", content="x", request_id=request_id)

    bus.resolve_response(request_id, msg)
    # Second resolve should return False (already cleaned up)
    assert bus.resolve_response(request_id, msg) is False
