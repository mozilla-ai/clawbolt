"""ChannelManager: lifecycle and routing for all enabled channels."""

import asyncio
import logging

from backend.app.channels.base import BaseChannel

logger = logging.getLogger(__name__)


class ChannelManager:
    """Start, stop, and route messages across all registered channels.

    Replaces the pattern of manually wiring each channel in ``main.py``.
    Each channel is registered via :meth:`register` and its lifecycle
    (start/stop) is coordinated through :meth:`start_all` / :meth:`stop_all`.
    """

    def __init__(self) -> None:
        self._channels: dict[str, BaseChannel] = {}

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

    # -- Lifecycle -------------------------------------------------------------

    async def start_all(self) -> list[asyncio.Task[None]]:
        """Start all registered channels concurrently.

        Returns a list of fire-and-forget tasks so callers can cancel them
        during shutdown if needed.
        """
        tasks: list[asyncio.Task[None]] = []
        for channel in self._channels.values():
            task = asyncio.create_task(channel.start())
            tasks.append(task)
            logger.info("Starting channel: %s", channel.name)
        return tasks

    async def stop_all(self) -> None:
        """Gracefully stop all registered channels."""
        for channel in self._channels.values():
            try:
                await channel.stop()
                logger.info("Stopped channel: %s", channel.name)
            except Exception:
                logger.exception("Error stopping channel %s", channel.name)
