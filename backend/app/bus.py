"""Async message bus for decoupled channel-agent communication.

Channels normalize inbound messages and publish them to the bus.
The ChannelManager consumes inbound messages, runs the agent pipeline,
and publishes outbound replies. An outbound dispatcher routes replies
back to the originating channel or resolves web chat response futures.

Follows nanobot's MessageBus pattern with added request-response
correlation for synchronous-feeling web chat via SSE.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.agent.ingestion import InboundMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboundMessage:
    """A reply from the agent destined for a specific channel."""

    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    request_id: str = ""
    is_typing_indicator: bool = False


class MessageBus:
    """Async message bus with inbound/outbound queues.

    Adds request-response correlation for web chat: callers register a
    future via ``register_response_future(request_id)`` and await the
    result; the outbound dispatcher resolves it via ``resolve_response()``.
    """

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._response_futures: dict[str, asyncio.Future[OutboundMessage]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    def register_response_future(
        self, request_id: str, ttl: float = 300
    ) -> asyncio.Future[OutboundMessage]:
        """Create and return a future that will hold the reply for *request_id*.

        A cleanup task removes the future after *ttl* seconds if it has not
        been resolved, preventing memory leaks when the SSE endpoint is never
        opened.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[OutboundMessage] = loop.create_future()
        self._response_futures[request_id] = fut

        async def _cleanup() -> None:
            await asyncio.sleep(ttl)
            stale = self._response_futures.pop(request_id, None)
            if stale is not None and not stale.done():
                stale.cancel()
                logger.debug("Cleaned up stale response future for request %s", request_id)

        task = loop.create_task(_cleanup())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return fut

    def resolve_response(self, request_id: str, msg: OutboundMessage) -> bool:
        """Resolve a pending response future. Returns True if found."""
        fut = self._response_futures.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)
            return True
        return False

    async def wait_for_response(self, request_id: str, timeout: float = 120) -> OutboundMessage:
        """Wait for the outbound reply matching *request_id*.

        Raises ``asyncio.TimeoutError`` if no reply arrives within *timeout* seconds.
        """
        fut = self._response_futures.get(request_id)
        if fut is None:
            fut = self.register_response_future(request_id)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._response_futures.pop(request_id, None)

    def reset(self) -> None:
        """Clear all queues and pending futures (used by test fixtures)."""
        self.inbound = asyncio.Queue()
        self.outbound = asyncio.Queue()
        self._response_futures.clear()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()


# Module-level singleton
message_bus = MessageBus()
