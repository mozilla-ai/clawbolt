"""Telegram channel: inbound webhook + outbound messaging."""

import asyncio
import hmac
import logging
import mimetypes
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from telegram import Bot
from telegram.constants import ChatAction

from backend.app.agent.file_store import get_idempotency_store
from backend.app.agent.ingestion import InboundMessage
from backend.app.bus import message_bus
from backend.app.channels.base import BaseChannel
from backend.app.config import Settings, get_effective_webhook_secret, settings
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


# ---------------------------------------------------------------------------
# Pydantic models for Telegram webhook payloads
# ---------------------------------------------------------------------------


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    username: str = ""


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramPhotoSize(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str = ""
    file_size: int = 0


class TelegramMediaFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str = ""
    mime_type: str = ""


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int = 0
    chat: TelegramChat | None = None
    from_user: TelegramUser | None = Field(default=None, alias="from")
    text: str = ""
    caption: str = ""
    photo: list[TelegramPhotoSize] = Field(default_factory=list)
    voice: TelegramMediaFile | None = None
    video: TelegramMediaFile | None = None
    video_note: TelegramMediaFile | None = None
    audio: TelegramMediaFile | None = None
    document: TelegramMediaFile | None = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int = 0
    message: TelegramMessage | None = None


class _InvalidSecret(Exception):
    pass


# ---------------------------------------------------------------------------
# Markdown -> Telegram MarkdownV2 conversion
# ---------------------------------------------------------------------------

# Characters that must be escaped in MarkdownV2 outside of code/pre blocks.
# See https://core.telegram.org/bots/api#markdownv2-style
_MDV2_ESCAPE_CHARS = r"_*[]()~`>#+=|{}.!-"
_MDV2_ESCAPE_RE = re.compile(r"([" + re.escape(_MDV2_ESCAPE_CHARS) + r"])")

# Characters that must be escaped inside code/pre blocks (only ` and \).
_MDV2_CODE_ESCAPE_RE = re.compile(r"([`\\])")

_FENCED_CODE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _escape_mdv2(text: str) -> str:
    """Escape special characters for MarkdownV2 outside code blocks."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def _escape_mdv2_code(text: str) -> str:
    """Escape special characters for MarkdownV2 inside code/pre blocks."""
    return _MDV2_CODE_ESCAPE_RE.sub(r"\\\1", text)


def markdown_to_telegram_mdv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2.

    Standard Markdown uses ``**bold**`` and ``*italic*``.
    Telegram MarkdownV2 uses ``*bold*`` and ``_italic_``, plus requires
    escaping many special characters in literal text.
    """
    # 1. Stash fenced code blocks (different escape rules)
    code_blocks: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        lang = m.group(1)
        code = _escape_mdv2_code(m.group(2).strip())
        code_blocks.append(f"```{lang}\n{code}\n```" if lang else f"```\n{code}\n```")
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = _FENCED_CODE_RE.sub(_stash_code, text)

    # 2. Stash inline code
    inline_codes: list[str] = []

    def _stash_inline(m: re.Match[str]) -> str:
        inline_codes.append(f"`{_escape_mdv2_code(m.group(1))}`")
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 3. Extract formatting spans before escaping
    bold_spans: list[str] = []

    def _stash_bold(m: re.Match[str]) -> str:
        bold_spans.append(m.group(1))
        return f"\x00BOLD{len(bold_spans) - 1}\x00"

    text = _BOLD_RE.sub(_stash_bold, text)

    italic_spans: list[str] = []

    def _stash_italic(m: re.Match[str]) -> str:
        italic_spans.append(m.group(1))
        return f"\x00ITALIC{len(italic_spans) - 1}\x00"

    text = _ITALIC_RE.sub(_stash_italic, text)

    link_spans: list[tuple[str, str]] = []

    def _stash_link(m: re.Match[str]) -> str:
        link_spans.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(link_spans) - 1}\x00"

    text = _LINK_RE.sub(_stash_link, text)

    heading_spans: list[str] = []

    def _stash_heading(m: re.Match[str]) -> str:
        heading_spans.append(m.group(1))
        return f"\x00HEADING{len(heading_spans) - 1}\x00"

    text = _HEADING_RE.sub(_stash_heading, text)

    # 4. Escape all special characters in the remaining literal text
    text = _escape_mdv2(text)

    # 5. Restore formatting with MarkdownV2 syntax
    for i, content in enumerate(heading_spans):
        text = text.replace(f"\x00HEADING{i}\x00", f"*{_escape_mdv2(content)}*")

    for i, content in enumerate(bold_spans):
        text = text.replace(f"\x00BOLD{i}\x00", f"*{_escape_mdv2(content)}*")

    for i, content in enumerate(italic_spans):
        text = text.replace(f"\x00ITALIC{i}\x00", f"_{_escape_mdv2(content)}_")

    for i, (label, url) in enumerate(link_spans):
        escaped_label = _escape_mdv2(label)
        # Inside (...) only ) and \ need escaping
        escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
        text = text.replace(f"\x00LINK{i}\x00", f"[{escaped_label}]({escaped_url})")

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", code)

    return text


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
    def extract_media(update: TelegramUpdate) -> list[tuple[str, str]]:
        """Extract media file_ids from a Telegram update.

        Returns a list of ``(file_id, mime_type)`` tuples.
        """
        msg = update.message
        if msg is None:
            return []
        media: list[tuple[str, str]] = []

        if msg.photo:
            largest = max(msg.photo, key=lambda p: p.file_size)
            if largest.file_id:
                media.append((largest.file_id, "image/jpeg"))

        if msg.voice and msg.voice.file_id:
            media.append((msg.voice.file_id, msg.voice.mime_type or "audio/ogg"))

        if msg.video and msg.video.file_id:
            media.append((msg.video.file_id, msg.video.mime_type or "video/mp4"))

        if msg.video_note and msg.video_note.file_id:
            media.append((msg.video_note.file_id, "video/mp4"))

        if msg.audio and msg.audio.file_id:
            media.append((msg.audio.file_id, msg.audio.mime_type or "audio/mpeg"))

        if msg.document and msg.document.file_id:
            media.append(
                (msg.document.file_id, msg.document.mime_type or "application/octet-stream")
            )

        if media:
            logger.debug(
                "Extracted %d media item(s): %s",
                len(media),
                [(file_id, mime_type) for file_id, mime_type in media],
            )
        return media

    def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return ``True`` if the sender passes the Telegram allowlist gate.

        In premium mode, approval is based on whether a ``ChannelRoute``
        exists for this sender. In OSS mode, the static allowlist setting
        is used: empty rejects all, ``"*"`` allows all, or a specific
        chat ID must match.
        """
        premium = self._check_premium_route(sender_id)
        if premium is not None:
            return premium

        allowed = settings.telegram_allowed_chat_id.strip()

        if not allowed:
            return False

        if allowed == "*":
            return True

        return sender_id == allowed

    @staticmethod
    def parse_update(update: TelegramUpdate) -> InboundMessage | None:
        """Parse a Telegram update into an ``InboundMessage``.

        Returns ``None`` if the update should be ignored.
        """
        msg = update.message
        if not msg:
            return None

        if not msg.chat:
            logger.warning("Telegram message missing chat.id, ignoring")
            return None

        chat_id = str(msg.chat.id)
        raw_text = msg.text or msg.caption or ""

        text = raw_text
        if raw_text.strip().lower() in ("/start", "/start@"):
            text = "Hi"
        elif raw_text.strip().startswith("/"):
            logger.debug("Ignoring unhandled bot command: %s", raw_text.strip().split()[0])
            return None

        username = msg.from_user.username if msg.from_user else ""

        media_items = TelegramChannel.extract_media(update)

        message_id = str(msg.message_id) if msg.message_id else ""
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
        router = APIRouter()
        channel = self

        @router.post("/webhooks/telegram")
        async def telegram_inbound(
            request: Request,
            _rate_limit: None = Depends(check_webhook_rate_limit),
        ) -> JSONResponse:
            """Receive inbound messages from Telegram."""
            try:
                TelegramChannel._validate_webhook_secret(request)
            except _InvalidSecret:
                return JSONResponse(content={"ok": True})

            try:
                raw: dict = await request.json()
            except ValueError:
                logger.warning("Telegram webhook received invalid JSON")
                return JSONResponse(content={"ok": True})

            try:
                update = TelegramUpdate.model_validate(raw)
            except ValidationError as exc:
                logger.warning("Telegram webhook payload failed validation")
                logger.debug("Validation details: %s", exc.errors())
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
            if inbound.external_message_id:
                idempotency = get_idempotency_store()
                if idempotency.has_seen(inbound.external_message_id):
                    logger.info("Duplicate webhook for %s, skipping", inbound.external_message_id)
                    return JSONResponse(content={"ok": True})
                await idempotency.mark_seen(inbound.external_message_id)

            await message_bus.publish_inbound(inbound)
            return JSONResponse(content={"ok": True})

        return router

    # -- Outbound --------------------------------------------------------------

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
        try:
            msg = await self.bot.send_message(
                chat_id=self._parse_chat_id(to),
                text=markdown_to_telegram_mdv2(body),
                parse_mode="MarkdownV2",
            )
        except Exception:
            # Fall back to plain text if MarkdownV2 conversion fails
            msg = await self.bot.send_message(chat_id=self._parse_chat_id(to), text=body)
        return str(msg.message_id)

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Download *media_url* and send it as a document or photo."""
        chat_id = self._parse_chat_id(to)

        local_path = Path(media_url)
        if local_path.is_file():
            data = await asyncio.to_thread(local_path.read_bytes)
            content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
            filename = local_path.name
        elif media_url.startswith("http://") or media_url.startswith("https://"):
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
        else:
            msg = (
                f"Cannot send media: '{media_url}' is not a reachable local file "
                f"and is not a valid URL (missing http:// or https:// protocol)"
            )
            raise ValueError(msg)

        caption_mdv2 = markdown_to_telegram_mdv2(body) if body else ""
        if content_type.startswith("image/"):
            msg = await self.bot.send_photo(
                chat_id=chat_id,
                photo=data,
                caption=caption_mdv2,
                filename=filename,
                parse_mode="MarkdownV2",
            )
        else:
            msg = await self.bot.send_document(
                chat_id=chat_id,
                document=data,
                caption=caption_mdv2,
                filename=filename,
                parse_mode="MarkdownV2",
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
