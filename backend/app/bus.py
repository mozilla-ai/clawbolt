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
from typing import TYPE_CHECKING, Any

from backend.app.config import settings

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
        self._event_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._request_owners: dict[str, str] = {}
        # User-level activity streams for the web dashboard.  Multiple
        # browser tabs can subscribe concurrently for the same user.
        self._activity_queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        # Last published activity event per user, replayed to new subscribers
        # so reconnecting clients immediately learn the current agent state.
        self._last_activity: dict[str, dict[str, Any]] = {}

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

    def set_request_owner(self, request_id: str, user_id: str) -> None:
        """Associate a request_id with the user who created it."""
        self._request_owners[request_id] = user_id

    def check_request_owner(self, request_id: str, user_id: str) -> bool:
        """Return True if *user_id* owns *request_id* (or if no owner is recorded)."""
        owner = self._request_owners.get(request_id)
        return owner is None or owner == user_id

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
            self._request_owners.pop(request_id, None)
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

    # -- Event queues for SSE streaming of intermediate events ----------------

    def register_event_queue(self, request_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Create (or return existing) queue for streaming intermediate events."""
        existing = self._event_queues.get(request_id)
        if existing is not None:
            return existing
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_queues[request_id] = queue
        return queue

    async def publish_event(self, request_id: str, event: dict[str, Any]) -> None:
        """Push an intermediate event to the SSE stream for *request_id*."""
        queue = self._event_queues.get(request_id)
        if queue is not None:
            await queue.put(event)

    def remove_event_queue(self, request_id: str) -> None:
        """Remove the event queue and owner mapping for *request_id*."""
        self._event_queues.pop(request_id, None)
        self._request_owners.pop(request_id, None)

    # -- User-level activity streams for the web dashboard -----------------

    def register_activity_queue(self, user_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Create a new activity queue for *user_id* and return it.

        Each call creates a fresh queue so multiple browser tabs can
        subscribe concurrently.  The last published activity event is
        replayed into the new queue so reconnecting clients immediately
        learn the current agent state (e.g. whether the agent finished
        while the SSE connection was down).
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._activity_queues.setdefault(user_id, set()).add(queue)
        last = self._last_activity.get(user_id)
        if last is not None:
            queue.put_nowait(last)
        return queue

    def remove_activity_queue(self, user_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a specific activity queue for *user_id*."""
        queues = self._activity_queues.get(user_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del self._activity_queues[user_id]

    async def publish_activity(self, user_id: str, event: dict[str, Any]) -> None:
        """Push an activity event to all connected dashboard clients for *user_id*."""
        self._last_activity[user_id] = event
        queues = self._activity_queues.get(user_id)
        if queues:
            # Snapshot to avoid RuntimeError if a queue is removed during iteration
            for queue in list(queues):
                await queue.put(event)

    def get_response_future(self, request_id: str) -> asyncio.Future[OutboundMessage] | None:
        """Return the pending response future for *request_id*, or ``None``."""
        return self._response_futures.get(request_id)

    async def wait_for_response(
        self, request_id: str, timeout: float | None = None
    ) -> OutboundMessage:
        """Wait for the outbound reply matching *request_id*.

        Raises ``asyncio.TimeoutError`` if no reply arrives within *timeout* seconds.
        """
        if timeout is None:
            timeout = settings.approval_timeout_seconds
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
        self._event_queues.clear()
        self._request_owners.clear()
        self._activity_queues.clear()
        self._last_activity.clear()

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
