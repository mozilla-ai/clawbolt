"""Telegram implementation of the MessagingService protocol."""

import mimetypes
from pathlib import Path

import httpx
from telegram import Bot

from backend.app.config import Settings, settings


class TelegramMessagingService:
    """Send messages via Telegram Bot API."""

    def __init__(self, bot_token: str = "", svc_settings: Settings | None = None) -> None:
        token = bot_token or (svc_settings.telegram_bot_token if svc_settings else "")
        self.bot = Bot(token=token)
        self._token = token

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
        """Download *media_url* and send it as a document or photo.

        Supports both HTTP(S) URLs and local file paths.
        """
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
