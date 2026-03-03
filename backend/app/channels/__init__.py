"""Channel registry and BaseChannel export."""

from backend.app.channels.base import BaseChannel

_active_channels: dict[str, BaseChannel] = {}


def register_channel(channel: BaseChannel) -> None:
    """Register a channel instance by its ``name``."""
    _active_channels[channel.name] = channel


def get_channel(name: str) -> BaseChannel:
    """Return a registered channel by name, or raise ``KeyError``."""
    return _active_channels[name]


def get_default_channel() -> BaseChannel:
    """Return the first registered channel (convenience for single-channel setups)."""
    if not _active_channels:
        msg = "No channels registered"
        raise RuntimeError(msg)
    return next(iter(_active_channels.values()))


__all__ = [
    "BaseChannel",
    "get_channel",
    "get_default_channel",
    "register_channel",
]
