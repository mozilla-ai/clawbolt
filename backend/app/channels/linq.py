"""Linq channel: inbound webhook + outbound messaging (iMessage/RCS/SMS)."""

import asyncio
import hashlib
import hmac
import logging
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.app.agent.file_store import get_idempotency_store
from backend.app.agent.ingestion import InboundMessage
from backend.app.bus import message_bus
from backend.app.channels.base import BaseChannel
from backend.app.config import settings
from backend.app.media.download import DownloadedMedia, generate_filename
from backend.app.services.rate_limiter import check_webhook_rate_limit
from backend.app.services.webhook import discover_tunnel_url, wait_for_dns

logger = logging.getLogger(__name__)

LINQ_API_BASE = "https://api.linqapp.com/api/partner/v3"
LINQ_SIGNATURE_HEADER = "X-Webhook-Signature"
LINQ_TIMESTAMP_HEADER = "X-Webhook-Timestamp"
REPLAY_WINDOW_SECONDS = 300  # 5 minutes
STARTUP_DELAY_SECONDS = 3


# ---------------------------------------------------------------------------
# Webhook auto-registration
# ---------------------------------------------------------------------------


async def register_linq_webhook(webhook_url: str) -> bool:
    """Create a Linq webhook subscription and store the signing secret.

    Calls ``POST /v3/webhook-subscriptions`` to register *webhook_url* for
    ``message.received`` events. If the response includes a signing secret,
    it is applied to the running settings so HMAC verification activates
    immediately.

    Returns ``True`` on success.
    """
    url = f"{LINQ_API_BASE}/webhook-subscriptions"
    payload = {
        "target_url": webhook_url,
        "subscribed_events": ["message.received"],
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.linq_api_token}",
                    "Content-Type": "application/json",
                },
                timeout=settings.http_timeout_seconds,
            )
            if resp.status_code >= 400:
                logger.error("Linq webhook registration failed: %s %s", resp.status_code, resp.text)
                return False

            data = resp.json()
            # Store signing secret if returned
            secret = data.get("signing_secret") or data.get("secret", "")
            if secret and not settings.linq_webhook_signing_secret:
                settings.linq_webhook_signing_secret = secret
                logger.info("Linq webhook signing secret auto-configured from API response")

            logger.info("Linq webhook registered: %s", webhook_url)
            return True
    except httpx.HTTPError:
        logger.exception("Failed to register Linq webhook")
        return False


# ---------------------------------------------------------------------------
# Pydantic models for Linq webhook payloads
# ---------------------------------------------------------------------------


class LinqMessagePart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = ""  # "text", "media", "link"
    value: str = ""
    url: str = ""
    mime_type: str = ""


class LinqHandle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    handle: str = ""
    service: str = ""
    is_me: bool = False


class LinqChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    is_group: bool = False
    owner_handle: LinqHandle | None = None


class LinqMessageData(BaseModel):
    """The ``data`` object inside a Linq webhook event."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    direction: str = ""  # "inbound" | "outbound"
    sender_handle: LinqHandle | None = None
    chat: LinqChat | None = None
    parts: list[LinqMessagePart] = []
    service: str = ""


class LinqWebhookPayload(BaseModel):
    """Linq webhook payload (2026-02-03 format)."""

    model_config = ConfigDict(extra="ignore")

    api_version: str = ""
    webhook_version: str = ""
    event_type: str = ""  # "message.received", etc.
    event_id: str = ""
    data: LinqMessageData | None = None


# ---------------------------------------------------------------------------
# Linq channel implementation
# ---------------------------------------------------------------------------


class LinqChannel(BaseChannel):
    """Linq implementation combining inbound webhooks and outbound sending."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # In-memory cache: phone_number -> chat_uuid
        self._chat_cache: dict[str, str] = {}

    @property
    def _http(self) -> httpx.AsyncClient:
        """Lazily create the httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=LINQ_API_BASE,
                headers={
                    "Authorization": f"Bearer {settings.linq_api_token}",
                    "Content-Type": "application/json",
                },
                timeout=settings.http_timeout_seconds,
            )
        return self._client

    # -- BaseChannel identity --------------------------------------------------

    @property
    def name(self) -> str:
        return "linq"

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Discover Cloudflare Tunnel URL and auto-register Linq webhook.

        Mirrors Telegram's auto-registration pattern: waits for the tunnel,
        then creates a webhook subscription via the Linq API. The signing
        secret from the response is stored for HMAC verification.
        """
        if not settings.linq_api_token:
            return

        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        tunnel_url = await discover_tunnel_url()
        if not tunnel_url:
            logger.debug("Cloudflare tunnel not detected: skipping Linq webhook auto-registration")
            return

        webhook_url = f"{tunnel_url}/api/webhooks/linq"

        if not await wait_for_dns(tunnel_url):
            logger.warning(
                "Tunnel hostname never became resolvable: skipping Linq webhook registration"
            )
            return

        ok = await register_linq_webhook(webhook_url)
        if ok:
            logger.info("Linq webhook auto-registered: %s", webhook_url)
        else:
            logger.warning("Failed to auto-register Linq webhook")

    async def stop(self) -> None:
        """Close the httpx client on shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Inbound ---------------------------------------------------------------

    @staticmethod
    def verify_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
        """Verify HMAC-SHA256 webhook signature with replay protection."""
        secret = settings.linq_webhook_signing_secret
        if not secret:
            return True  # No secret configured: skip verification

        # Replay protection
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False
        if abs(time.time() - ts) > REPLAY_WINDOW_SECONDS:
            return False

        expected = hmac.new(
            key=secret.encode(),
            msg=f"{timestamp}.{raw_body.decode()}".encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return True if the sender passes the Linq allowlist.

        ``sender_id`` is the phone number in E.164 format.
        ``settings.linq_allowed_numbers`` can be empty (deny all),
        ``"*"`` (allow all), or a specific E.164 number.
        """
        allowed = settings.linq_allowed_numbers.strip()
        if not allowed:
            return False
        if allowed == "*":
            return True
        return sender_id == allowed

    @staticmethod
    def _guess_mime(url: str) -> str:
        """Guess MIME type from a CDN URL extension."""
        url_lower = url.lower()
        if any(url_lower.endswith(ext) for ext in (".jpg", ".jpeg")):
            return "image/jpeg"
        if url_lower.endswith(".png"):
            return "image/png"
        if url_lower.endswith(".gif"):
            return "image/gif"
        if url_lower.endswith(".mp4"):
            return "video/mp4"
        if url_lower.endswith(".mp3"):
            return "audio/mpeg"
        if url_lower.endswith(".ogg"):
            return "audio/ogg"
        if url_lower.endswith(".pdf"):
            return "application/pdf"
        return "application/octet-stream"

    @staticmethod
    def parse_webhook(payload: LinqWebhookPayload) -> InboundMessage | None:
        """Parse a Linq webhook payload into an InboundMessage.

        Returns None if the payload should be ignored.
        """
        if payload.event_type != "message.received":
            return None

        data = payload.data
        if not data:
            return None

        if data.direction == "outbound":
            return None

        sender = data.sender_handle
        if not sender or not sender.handle:
            logger.warning("Linq message missing sender_handle, ignoring")
            return None

        text_parts: list[str] = []
        media_refs: list[tuple[str, str]] = []

        for part in data.parts:
            if part.type == "text" and part.value:
                text_parts.append(part.value)
            elif part.type == "media" and part.url:
                mime = part.mime_type or LinqChannel._guess_mime(part.url)
                media_refs.append((part.url, mime))

        text = " ".join(text_parts)
        external_id = f"linq_{data.id}" if data.id else ""

        return InboundMessage(
            channel="linq",
            sender_id=sender.handle,
            text=text,
            media_refs=media_refs,
            external_message_id=external_id,
            sender_username=None,
        )

    def get_router(self) -> APIRouter:
        """Build a router with the Linq webhook endpoint."""
        router = APIRouter()
        channel = self

        @router.post("/webhooks/linq")
        async def linq_inbound(
            request: Request,
            _rate_limit: None = Depends(check_webhook_rate_limit),
        ) -> JSONResponse:
            """Receive inbound messages from Linq."""
            raw_body = await request.body()
            timestamp = request.headers.get(LINQ_TIMESTAMP_HEADER, "")
            signature = request.headers.get(LINQ_SIGNATURE_HEADER, "")

            if not LinqChannel.verify_signature(raw_body, timestamp, signature):
                logger.warning("Invalid Linq webhook signature")
                return JSONResponse(content={"ok": True})

            try:
                raw: dict = await request.json()
            except ValueError:
                logger.warning("Linq webhook received invalid JSON")
                return JSONResponse(content={"ok": True})

            logger.info("Linq webhook raw payload: %s", raw)

            try:
                payload = LinqWebhookPayload.model_validate(raw)
            except Exception:
                logger.warning("Linq webhook payload failed validation")
                return JSONResponse(content={"ok": True})

            data = payload.data
            logger.info(
                "Linq webhook parsed: event_type=%s direction=%s sender=%s parts=%d",
                payload.event_type,
                data.direction if data else "",
                data.sender_handle.handle if data and data.sender_handle else "",
                len(data.parts) if data else 0,
            )

            inbound = LinqChannel.parse_webhook(payload)
            if inbound is None:
                logger.info("Linq parse_webhook returned None, skipping")
                return JSONResponse(content={"ok": True})

            if not channel.is_allowed(inbound.sender_id, ""):
                logger.info("Phone %s not in Linq allowlist, ignoring", inbound.sender_id)
                return JSONResponse(content={"ok": True})

            # Cache the chat_id for outbound use
            if data and data.chat and data.chat.id and data.sender_handle:
                channel._chat_cache[data.sender_handle.handle] = data.chat.id

            # Idempotency: skip duplicate messages
            if inbound.external_message_id:
                idempotency = get_idempotency_store()
                if idempotency.has_seen(inbound.external_message_id):
                    logger.info(
                        "Duplicate Linq webhook for %s, skipping", inbound.external_message_id
                    )
                    return JSONResponse(content={"ok": True})
                await idempotency.mark_seen(inbound.external_message_id)

            await message_bus.publish_inbound(inbound)
            return JSONResponse(content={"ok": True})

        return router

    # -- Outbound --------------------------------------------------------------

    async def _send_to_linq(
        self,
        phone: str,
        parts: list[dict[str, str]],
    ) -> str:
        """Send message parts to a phone number via Linq API.

        Uses cached chat_id if available, otherwise creates a new chat.
        Returns the message ID from the Linq API response.
        """
        cached_chat_id = self._chat_cache.get(phone)

        if cached_chat_id:
            resp = await self._http.post(
                f"/chats/{cached_chat_id}/messages",
                json={"message": {"parts": parts}},
            )
        else:
            resp = await self._http.post(
                "/chats",
                json={
                    "from": settings.linq_from_number,
                    "to": [phone],
                    "message": {"parts": parts},
                },
            )

        if resp.status_code >= 400:
            logger.error(
                "Linq send failed: %s %s %s", resp.status_code, resp.request.url, resp.text
            )
        resp.raise_for_status()
        data = resp.json()

        # Cache the chat_id from the response
        chat_id = data.get("chat_id", "")
        if chat_id:
            self._chat_cache[phone] = chat_id

        return data.get("message_id", "")

    async def send_text(self, to: str, body: str) -> str:
        """Send a text message. *to* is a phone number in E.164 format."""
        parts = [{"type": "text", "value": body}]
        return await self._send_to_linq(to, parts)

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send a message with a media attachment."""
        parts: list[dict[str, str]] = []
        if body:
            parts.append({"type": "text", "value": body})
        parts.append({"type": "media", "url": media_url})
        return await self._send_to_linq(to, parts)

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send a text or media message, combining multiple media into one multi-part message."""
        if not media_urls:
            return await self.send_text(to, body)

        parts: list[dict[str, str]] = []
        if body:
            parts.append({"type": "text", "value": body})
        for url in media_urls:
            parts.append({"type": "media", "url": url})
        return await self._send_to_linq(to, parts)

    async def send_typing_indicator(self, to: str) -> None:
        """Send a typing indicator via Linq API."""
        cached_chat_id = self._chat_cache.get(to)
        if not cached_chat_id:
            return
        try:
            await self._http.post(f"/chats/{cached_chat_id}/typing")
        except Exception:
            logger.debug("Failed to send Linq typing indicator to %s", to)

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media from a Linq CDN URL.

        For Linq, ``file_id`` is the full CDN URL from the webhook payload.
        """
        resp = await self._http.get(file_id, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]

        size_bytes = len(resp.content)
        if size_bytes > settings.max_media_size_bytes:
            msg = (
                f"Media file too large: {size_bytes} bytes "
                f"(limit {settings.max_media_size_bytes} bytes)"
            )
            raise ValueError(msg)

        filename = generate_filename(content_type)
        return DownloadedMedia(
            content=resp.content,
            mime_type=content_type,
            original_url=file_id,
            filename=filename,
        )
