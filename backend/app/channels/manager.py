"""ChannelManager: lifecycle, routing, and bus consumer/dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from backend.app.media.download import DownloadedMedia

if TYPE_CHECKING:
    from backend.app.agent.ingestion import InboundMessage

from backend.app.channels.base import BaseChannel

logger = logging.getLogger(__name__)


class ChannelManager:
    """Start, stop, and route messages across all registered channels.

    In addition to managing channel lifecycles, runs two long-lived tasks:

    * **inbound consumer**: reads from the message bus, performs user
      lookup / session creation / persistence, and dispatches to the agent
      pipeline (with per-user locking).
    * **outbound dispatcher**: reads agent replies from the bus and routes
      them to the correct channel's send method or resolves web chat
      response futures.
    """

    def __init__(self) -> None:
        self._channels: dict[str, BaseChannel] = {}
        self._bus_tasks: list[asyncio.Task[None]] = []

    # -- Registration ----------------------------------------------------------

    def register(self, channel: BaseChannel) -> None:
        """Register a channel instance by its ``name``."""
        if channel.name in self._channels:
            msg = f"Channel {channel.name!r} is already registered"
            raise ValueError(msg)
        self._channels[channel.name] = channel
        logger.info("Registered channel: %s", channel.name)

    # -- Lookup ----------------------------------------------------------------

    @property
    def channels(self) -> dict[str, BaseChannel]:
        """Return a read-only view of registered channels."""
        return dict(self._channels)

    def get(self, name: str) -> BaseChannel:
        """Return a channel by name, or raise ``KeyError``."""
        return self._channels[name]

    def get_default(self) -> BaseChannel:
        """Return the first registered channel (single-channel convenience)."""
        if not self._channels:
            msg = "No channels registered"
            raise RuntimeError(msg)
        return next(iter(self._channels.values()))

    # -- Bus consumer / dispatcher ---------------------------------------------

    async def _run_inbound_consumer(self) -> None:
        """Loop: consume inbound messages from the bus and dispatch processing."""
        from backend.app.bus import message_bus

        bg_tasks: set[asyncio.Task[None]] = set()

        while True:
            try:
                inbound = await message_bus.consume_inbound()
            except asyncio.CancelledError:
                break

            # Dispatch each message as its own task so the consumer is not
            # blocked while the agent pipeline runs.
            task = asyncio.create_task(
                self._handle_inbound(inbound),
            )
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

    async def _handle_inbound(
        self,
        inbound: InboundMessage,
    ) -> None:
        """Process a single inbound message (runs as an asyncio task)."""
        from backend.app.agent.ingestion import process_inbound_from_bus

        # Extract download_media callback from the channel that sent the
        # message if available, otherwise fall back to the default channel.
        channel = self._channels.get(inbound.channel)
        if channel is None:
            channel = self.get_default()

        download_media: Callable[[str], Awaitable[DownloadedMedia]] = channel.download_media

        try:
            await process_inbound_from_bus(inbound, download_media=download_media)
        except Exception:
            logger.exception(
                "Failed to process inbound message from %s/%s",
                inbound.channel,
                inbound.sender_id,
            )

    async def _run_outbound_dispatcher(self) -> None:
        """Loop: consume outbound messages and route to channels."""
        from backend.app.bus import message_bus

        while True:
            try:
                outbound = await message_bus.consume_outbound()
            except asyncio.CancelledError:
                break

            # Try to resolve a web chat response future first
            if outbound.request_id:
                resolved = message_bus.resolve_response(outbound.request_id, outbound)
                if resolved:
                    continue

            # Otherwise send via the channel's outbound methods
            channel = self._channels.get(outbound.channel)
            if channel is None:
                logger.warning(
                    "No channel %r registered for outbound message to %s",
                    outbound.channel,
                    outbound.chat_id,
                )
                continue

            try:
                if outbound.is_typing_indicator:
                    await channel.send_typing_indicator(to=outbound.chat_id)
                elif outbound.media:
                    await channel.send_message(
                        to=outbound.chat_id,
                        body=outbound.content,
                        media_urls=outbound.media,
                    )
                else:
                    await channel.send_text(to=outbound.chat_id, body=outbound.content)
            except Exception:
                logger.exception(
                    "Failed to send outbound message via %s to %s",
                    outbound.channel,
                    outbound.chat_id,
                )

    # -- Lifecycle -------------------------------------------------------------

    async def start_all(self) -> list[asyncio.Task[None]]:
        """Start all registered channels and bus consumer/dispatcher.

        Returns a list of channel-start tasks (short-lived) so callers can
        cancel them during shutdown if needed.  Bus tasks are long-running
        loops managed internally via ``self._bus_tasks`` and cancelled by
        ``stop_all()``.
        """
        tasks: list[asyncio.Task[None]] = []
        for channel in self._channels.values():
            task = asyncio.create_task(channel.start())
            tasks.append(task)
            logger.info("Starting channel: %s", channel.name)

        # Start bus consumer and outbound dispatcher (managed internally)
        inbound_task = asyncio.create_task(self._run_inbound_consumer())
        outbound_task = asyncio.create_task(self._run_outbound_dispatcher())
        self._bus_tasks = [inbound_task, outbound_task]
        logger.info("Started message bus consumer and outbound dispatcher")

        return tasks

    async def stop_all(self) -> None:
        """Gracefully stop all registered channels and bus tasks."""
        # Cancel bus tasks first
        for task in self._bus_tasks:
            if not task.done():
                task.cancel()
        for task in self._bus_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bus_tasks.clear()
        logger.info("Stopped message bus consumer and outbound dispatcher")

        for channel in self._channels.values():
            try:
                await channel.stop()
                logger.info("Stopped channel: %s", channel.name)
            except Exception:
                logger.exception("Error stopping channel %s", channel.name)
