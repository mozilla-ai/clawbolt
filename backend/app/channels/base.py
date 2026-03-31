"""Abstract base class for messaging channels."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.app.agent.file_store import get_idempotency_store
from backend.app.bus import message_bus
from backend.app.media.download import DownloadedMedia

if TYPE_CHECKING:
    from backend.app.agent.ingestion import InboundMessage

logger = logging.getLogger(__name__)

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

    def _check_static_allowlist(self, setting_value: str, sender_id: str) -> bool:
        """Check premium route first, then fall back to a static allowlist setting.

        Consolidates the common pattern used by Telegram, BlueBubbles, and Linq:
        premium override -> empty denies all -> ``"*"`` allows all -> exact match.
        """
        premium = self._check_premium_route(sender_id)
        if premium is not None:
            return premium
        allowed = setting_value.strip()
        if not allowed:
            return False
        if allowed == "*":
            return True
        return sender_id == allowed

    # -- Outbound --------------------------------------------------------------

    @abstractmethod
    async def send_text(self, to: str, body: str) -> str:
        """Send a text message. Returns an external message ID."""

    @abstractmethod
    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send a message with a media attachment. Returns an external message ID."""

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send a text or media message. Returns an external message ID.

        Default implementation delegates to ``send_media`` for each URL (first
        URL carries the body as caption) or ``send_text`` when no media is
        provided. Channels that need different behavior (e.g. Linq's
        multi-part API) can override.
        """
        if media_urls:
            last_id = ""
            for i, url in enumerate(media_urls):
                caption = body if i == 0 else ""
                last_id = await self.send_media(to, caption, url)
            return last_id
        return await self.send_text(to, body)

    @abstractmethod
    async def send_typing_indicator(self, to: str) -> None:
        """Send a typing indicator to show the bot is processing."""

    @abstractmethod
    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media by channel-specific file identifier."""


async def handle_webhook_inbound(
    channel: BaseChannel,
    inbound: InboundMessage | None,
    *,
    on_accepted: Callable[[], None] | None = None,
) -> JSONResponse:
    """Shared post-parse logic for webhook-based channels.

    After a channel parses its provider-specific payload into an
    ``InboundMessage``, this helper runs the common steps: allowlist
    check, idempotency dedup, and bus publish.

    *on_accepted* is called after the allowlist check passes, before
    idempotency and publish. Use it for side effects that should only
    run for allowed senders (e.g. populating a chat-ID cache).
    """
    if inbound is None:
        return JSONResponse(content={"ok": True})

    if not channel.is_allowed(inbound.sender_id, inbound.sender_username or ""):
        logger.debug(
            "%s: sender not in allowlist, ignoring",
            channel.name,
        )
        return JSONResponse(content={"ok": True})

    if on_accepted is not None:
        try:
            on_accepted()
        except Exception:
            logger.exception("%s: on_accepted callback failed", channel.name)

    if inbound.external_message_id:
        idempotency = get_idempotency_store()
        if not idempotency.try_mark_seen(inbound.external_message_id):
            logger.info(
                "%s: duplicate webhook for %s, skipping",
                channel.name,
                inbound.external_message_id,
            )
            return JSONResponse(content={"ok": True})

    logger.info(
        "%s: inbound accepted, extId=%s",
        channel.name,
        inbound.external_message_id,
    )
    await message_bus.publish_inbound(inbound)
    return JSONResponse(content={"ok": True})
