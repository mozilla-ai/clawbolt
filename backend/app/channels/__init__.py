"""Channel registry and BaseChannel export."""

from backend.app.channels.base import BaseChannel
from backend.app.channels.manager import ChannelManager

_manager = ChannelManager()


def get_manager() -> ChannelManager:
    """Return the module-level ChannelManager singleton."""
    return _manager


def register_channel(channel: BaseChannel) -> None:
    """Register a channel instance by its ``name``."""
    _manager.register(channel)


def get_channel(name: str) -> BaseChannel:
    """Return a registered channel by name, or raise ``KeyError``."""
    return _manager.get(name)


def get_default_channel() -> BaseChannel:
    """Return the first registered channel (convenience for single-channel setups)."""
    return _manager.get_default()


__all__ = [
    "BaseChannel",
    "ChannelManager",
    "get_channel",
    "get_default_channel",
    "get_manager",
    "register_channel",
]
