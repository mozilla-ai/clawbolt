"""Abstract base class for messaging channels."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from fastapi import APIRouter

from backend.app.media.download import DownloadedMedia

# Type for the pluggable allowlist override. The callback receives
# (channel_name, sender_id) and returns True/False. When set, channels
# delegate to this instead of their static allowlist.
IsAllowedOverride = Callable[[str, str], bool]

# Module-level override set by premium during plugin initialization.
_is_allowed_override: IsAllowedOverride | None = None


def set_is_allowed_override(fn: IsAllowedOverride) -> None:
    """Register a global allowlist override (called by the premium plugin)."""
    global _is_allowed_override
    _is_allowed_override = fn


def get_is_allowed_override() -> IsAllowedOverride | None:
    """Return the current allowlist override, or None if not set."""
    return _is_allowed_override


class BaseChannel(ABC):
    """Unified inbound + outbound channel interface.

    Each channel (Telegram, SMS, web chat, ...) subclasses ``BaseChannel``
    and provides both inbound webhook parsing and outbound message sending.
    The outbound dispatcher in ``ChannelManager`` calls the five outbound
    methods (``send_text``, ``send_media``, ``send_message``,
    ``send_typing_indicator``, ``download_media``) to deliver messages.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this channel (e.g. ``"telegram"``)."""

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:  # noqa: B027
        """Hook called once after the ASGI app is ready to accept traffic."""

    async def stop(self) -> None:  # noqa: B027
        """Hook called during server shutdown."""

    # -- Inbound ---------------------------------------------------------------

    @abstractmethod
    def get_router(self) -> APIRouter:
        """Return a FastAPI ``APIRouter`` that handles inbound webhooks."""

    @abstractmethod
    def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return ``True`` if the sender passes the channel's allowlist."""

    def _check_premium_route(self, sender_id: str) -> bool | None:
        """Check if a plugin-level allowlist override handles this sender.

        Returns ``True``/``False`` if an override is registered (e.g. premium
        checks ``ChannelRoute`` existence), or ``None`` if no override is set
        (caller should fall through to its own static allowlist logic).
        """
        override = _is_allowed_override
        if override is None:
            return None
        return override(self.name, sender_id)

    # -- Outbound --------------------------------------------------------------

    @abstractmethod
    async def send_text(self, to: str, body: str) -> str:
        """Send a text message. Returns an external message ID."""

    @abstractmethod
    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send a message with a media attachment. Returns an external message ID."""

    @abstractmethod
    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send a text or media message. Returns an external message ID."""

    @abstractmethod
    async def send_typing_indicator(self, to: str) -> None:
        """Send a typing indicator to show the bot is processing."""

    @abstractmethod
    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media by channel-specific file identifier."""
