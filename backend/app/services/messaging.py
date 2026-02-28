"""Channel-agnostic messaging protocol and factory."""

from collections.abc import Generator
from typing import Protocol, runtime_checkable

from backend.app.config import settings


@runtime_checkable
class MessagingService(Protocol):
    """Channel-agnostic messaging interface. Implement for each channel."""

    async def send_text(self, to: str, body: str) -> str:
        """Send a text message. Returns an external message ID."""
        ...

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send a message with a media attachment. Returns an external message ID."""
        ...

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send a text or media message. Returns an external message ID."""
        ...


def _build_messaging_service() -> MessagingService:
    """Build the configured messaging service."""
    provider = settings.messaging_provider.lower()
    if provider == "telegram":
        from backend.app.services.telegram_service import TelegramMessagingService

        return TelegramMessagingService(bot_token=settings.telegram_bot_token)
    msg = f"Unknown messaging provider: {provider}"
    raise ValueError(msg)


def get_messaging_service() -> Generator[MessagingService]:
    """FastAPI dependency for MessagingService (overridable in tests)."""
    yield _build_messaging_service()
