"""Channel registry and BaseChannel export."""

from backend.app.channels.base import BaseChannel
from backend.app.channels.manager import ChannelManager
from backend.app.config import settings

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


def is_bluebubbles_configured() -> bool:
    """Check if BlueBubbles is configured AND the server is reachable."""
    if not settings.bluebubbles_server_url or not settings.bluebubbles_password:
        return False
    try:
        from backend.app.channels.bluebubbles import BlueBubblesChannel

        ch = _manager.get("bluebubbles")
        if isinstance(ch, BlueBubblesChannel):
            return ch.server_reachable
    except KeyError:
        pass
    return False


def reset_channel_clients(updates: dict[str, object]) -> None:
    """Reset live channel HTTP clients after credential changes.

    Call after updating settings so channels pick up new tokens/URLs.
    """
    if "telegram_bot_token" in updates:
        try:
            from backend.app.channels.telegram import TelegramChannel

            channel = _manager.get("telegram")
            if isinstance(channel, TelegramChannel):
                channel._token = settings.telegram_bot_token
                channel._bot = None
        except KeyError:
            pass

    if "linq_api_token" in updates:
        try:
            from backend.app.channels.linq import LinqChannel

            channel = _manager.get("linq")
            if isinstance(channel, LinqChannel):
                channel._client = None
        except KeyError:
            pass

    if "bluebubbles_server_url" in updates or "bluebubbles_password" in updates:
        try:
            from backend.app.channels.bluebubbles import BlueBubblesChannel

            channel = _manager.get("bluebubbles")
            if isinstance(channel, BlueBubblesChannel):
                channel._client = None
        except KeyError:
            pass


__all__ = [
    "BaseChannel",
    "ChannelManager",
    "get_channel",
    "get_default_channel",
    "get_manager",
    "is_bluebubbles_configured",
    "register_channel",
    "reset_channel_clients",
]
