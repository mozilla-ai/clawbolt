"""Abstract base class for messaging channels."""

from abc import ABC, abstractmethod

from fastapi import APIRouter

from backend.app.media.download import DownloadedMedia


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
