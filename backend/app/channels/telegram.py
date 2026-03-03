"""Telegram channel: inbound webhook + outbound messaging."""

import asyncio
import hmac
import logging
import mimetypes
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from telegram import Bot
from telegram.constants import ChatAction

from backend.app.agent.ingestion import InboundMessage, process_inbound_message
from backend.app.channels.base import BaseChannel
from backend.app.config import Settings, get_effective_webhook_secret, settings
from backend.app.database import get_db
from backend.app.media.download import DownloadedMedia, download_telegram_media
from backend.app.services.rate_limiter import check_webhook_rate_limit
from backend.app.services.webhook import (
    discover_tunnel_url,
    register_telegram_webhook,
    wait_for_dns,
)

logger = logging.getLogger(__name__)

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
STARTUP_DELAY_SECONDS = 3


class _InvalidSecret(Exception):
    pass


class TelegramChannel(BaseChannel):
    """Telegram implementation combining inbound webhooks and outbound sending."""

    def __init__(self, bot_token: str = "", svc_settings: Settings | None = None) -> None:
        self._token = bot_token or (svc_settings.telegram_bot_token if svc_settings else "")
        self._bot: Bot | None = None

    @property
    def bot(self) -> Bot:
        """Lazily create the Bot instance (avoids InvalidToken on empty token at import time)."""
        if self._bot is None:
            self._bot = Bot(token=self._token)
        return self._bot

    @bot.setter
    def bot(self, value: Bot) -> None:
        self._bot = value

    # -- BaseChannel identity --------------------------------------------------

    @property
    def name(self) -> str:
        return "telegram"

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Discover Cloudflare Tunnel URL and register Telegram webhook.

        Runs as a fire-and-forget task after the server is listening so that
        Telegram can reach the webhook URL during its validation check.
        """
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        tunnel_url = await discover_tunnel_url()
        if not tunnel_url:
            logger.debug("Cloudflare tunnel not detected: skipping webhook auto-registration")
            return

        webhook_url = f"{tunnel_url}/api/webhooks/telegram"
        secret = get_effective_webhook_secret(settings) or None

        if not await wait_for_dns(tunnel_url):
            logger.warning("Tunnel hostname never became resolvable: skipping webhook registration")
            return

        ok = await register_telegram_webhook(
            settings.telegram_bot_token, webhook_url, secret=secret
        )
        if ok:
            logger.info("Telegram webhook auto-registered: %s", webhook_url)
        else:
            logger.warning("Failed to auto-register Telegram webhook")

    # -- Inbound ---------------------------------------------------------------

    @staticmethod
    def _validate_webhook_secret(request: Request) -> None:
        """Validate the Telegram webhook secret token header."""
        secret = get_effective_webhook_secret(settings)
        if not secret:
            return
        header = request.headers.get(TELEGRAM_SECRET_HEADER, "")
        if not hmac.compare_digest(header, secret):
            logger.warning("Invalid Telegram webhook secret")
            raise _InvalidSecret

    @staticmethod
    def extract_media(update: dict) -> list[tuple[str, str]]:
        """Extract media file_ids from a Telegram update.

        Returns a list of ``(file_id, mime_type)`` tuples.
        """
        msg = update.get("message", {})
        media: list[tuple[str, str]] = []

        photos = msg.get("photo")
        if photos:
            largest = max(photos, key=lambda p: p.get("file_size", 0))
            file_id = largest.get("file_id")
            if file_id:
                media.append((file_id, "image/jpeg"))

        voice = msg.get("voice")
        if voice:
            file_id = voice.get("file_id")
            if file_id:
                media.append((file_id, voice.get("mime_type", "audio/ogg")))

        video = msg.get("video")
        if video:
            file_id = video.get("file_id")
            if file_id:
                media.append((file_id, video.get("mime_type", "video/mp4")))

        video_note = msg.get("video_note")
        if video_note:
            file_id = video_note.get("file_id")
            if file_id:
                media.append((file_id, "video/mp4"))

        audio = msg.get("audio")
        if audio:
            file_id = audio.get("file_id")
            if file_id:
                media.append((file_id, audio.get("mime_type", "audio/mpeg")))

        doc = msg.get("document")
        if doc:
            file_id = doc.get("file_id")
            if file_id:
                doc_mime = doc.get("mime_type", "application/octet-stream")
                media.append((file_id, doc_mime))

        if media:
            logger.debug(
                "Extracted %d media item(s): %s",
                len(media),
                [(file_id, mime_type) for file_id, mime_type in media],
            )
        return media

    def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return ``True`` if the sender passes the Telegram allowlist gate.

        Both lists default to empty, which rejects all senders (deny by default).
        Set a list to ``"*"`` to explicitly allow everyone through that check.
        """
        ids_raw = settings.telegram_allowed_chat_ids.strip()
        users_raw = settings.telegram_allowed_usernames.strip()

        if not ids_raw and not users_raw:
            return False

        chat_id_match = False
        username_match = False

        if ids_raw == "*":
            chat_id_match = True
        elif ids_raw:
            allowed_ids = {cid.strip() for cid in ids_raw.split(",")}
            chat_id_match = sender_id in allowed_ids

        if users_raw == "*":
            username_match = True
        elif users_raw:
            allowed_users = {u.strip().lstrip("@").lower() for u in users_raw.split(",")}
            username_match = username.lower() in allowed_users if username else False

        return chat_id_match or username_match

    @staticmethod
    def parse_update(update: dict) -> InboundMessage | None:
        """Parse a Telegram update dict into an ``InboundMessage``.

        Returns ``None`` if the update should be ignored.
        """
        msg = update.get("message")
        if not msg:
            return None

        chat = msg.get("chat") if isinstance(msg, dict) else None
        if not chat or "id" not in chat:
            logger.warning("Telegram message missing chat.id, ignoring")
            return None

        chat_id = str(chat["id"])
        raw_text = msg.get("text") or msg.get("caption") or ""

        text = raw_text
        if raw_text.strip().lower() in ("/start", "/start@"):
            text = "Hi"
        elif raw_text.strip().startswith("/"):
            logger.debug("Ignoring unhandled bot command: %s", raw_text.strip().split()[0])
            return None

        username = msg.get("from", {}).get("username", "")

        media_items = TelegramChannel.extract_media(update)

        message_id = str(msg.get("message_id", ""))
        external_id = f"tg_{chat_id}_{message_id}" if message_id else ""

        return InboundMessage(
            channel="telegram",
            sender_id=chat_id,
            text=text,
            media_refs=media_items,
            external_message_id=external_id,
            sender_username=username or None,
        )

    def get_router(self) -> APIRouter:
        """Build a router with the Telegram webhook endpoint."""
        from backend.app.services.messaging import MessagingService, get_messaging_service

        router = APIRouter()
        channel = self

        @router.post("/webhooks/telegram")
        async def telegram_inbound(
            request: Request,
            _rate_limit: None = Depends(check_webhook_rate_limit),
            db: Session = Depends(get_db),
            messaging_service: MessagingService = Depends(get_messaging_service),
        ) -> JSONResponse:
            """Receive inbound messages from Telegram."""
            try:
                TelegramChannel._validate_webhook_secret(request)
            except _InvalidSecret:
                return JSONResponse(content={"ok": True})

            try:
                update: dict = await request.json()
            except ValueError:
                logger.warning("Telegram webhook received invalid JSON")
                return JSONResponse(content={"ok": True})

            inbound = TelegramChannel.parse_update(update)
            if inbound is None:
                return JSONResponse(content={"ok": True})

            if not channel.is_allowed(inbound.sender_id, inbound.sender_username or ""):
                logger.info(
                    "Chat %s / @%s not in allowlist, ignoring",
                    inbound.sender_id,
                    inbound.sender_username or "",
                )
                return JSONResponse(content={"ok": True})

            # Idempotency: skip duplicate updates
            from backend.app.models import Message

            if inbound.external_message_id:
                existing = (
                    db.query(Message)
                    .filter(Message.external_message_id == inbound.external_message_id)
                    .first()
                )
                if existing:
                    logger.info("Duplicate webhook for %s, skipping", inbound.external_message_id)
                    return JSONResponse(content={"ok": True})

            task, _contractor, _message = await process_inbound_message(
                db, inbound, messaging_service
            )
            return JSONResponse(content={"ok": True}, background=task)

        return router

    # -- Outbound (MessagingService protocol) ----------------------------------

    @staticmethod
    def _parse_chat_id(to: str) -> int:
        """Parse a Telegram chat_id from a string, stripping phone-number prefixes."""
        cleaned = to.lstrip("+")
        try:
            return int(cleaned)
        except (ValueError, TypeError) as exc:
            msg = f"Invalid Telegram chat_id: {to!r}"
            raise ValueError(msg) from exc

    async def send_text(self, to: str, body: str) -> str:
        """Send a text message. *to* is a Telegram chat_id."""
        msg = await self.bot.send_message(chat_id=self._parse_chat_id(to), text=body)
        return str(msg.message_id)

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Download *media_url* and send it as a document or photo."""
        chat_id = self._parse_chat_id(to)

        local_path = Path(media_url)
        if local_path.is_file():
            data = local_path.read_bytes()
            content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            filename = local_path.name
        else:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    media_url, follow_redirects=True, timeout=settings.http_timeout_seconds
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "application/octet-stream").split(
                    ";"
                )[0]
                ext = mimetypes.guess_extension(content_type) or ".bin"
                filename = f"file{ext}"
                data = resp.content

        if content_type.startswith("image/"):
            msg = await self.bot.send_photo(
                chat_id=chat_id, photo=data, caption=body, filename=filename
            )
        else:
            msg = await self.bot.send_document(
                chat_id=chat_id, document=data, caption=body, filename=filename
            )
        return str(msg.message_id)

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send text or media based on whether media_urls is provided."""
        if media_urls:
            last_id = ""
            for i, url in enumerate(media_urls):
                caption = body if i == 0 else ""
                last_id = await self.send_media(to, caption, url)
            return last_id
        return await self.send_text(to, body)

    async def send_typing_indicator(self, to: str) -> None:
        """Send 'typing...' chat action to Telegram."""
        try:
            await self.bot.send_chat_action(
                chat_id=self._parse_chat_id(to), action=ChatAction.TYPING
            )
        except Exception:
            logger.debug("Failed to send typing indicator to %s", to)

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media from Telegram via the Bot API."""
        return await download_telegram_media(file_id, bot_token=self._token)
