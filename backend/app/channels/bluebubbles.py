"""BlueBubbles channel: inbound webhook + outbound messaging (iMessage via self-hosted Mac bridge)."""

import asyncio
import hmac
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.agent.file_store import get_idempotency_store
from backend.app.agent.ingestion import InboundMessage
from backend.app.bus import message_bus
from backend.app.channels.base import BaseChannel
from backend.app.config import settings
from backend.app.media.download import DownloadedMedia, generate_filename
from backend.app.services.rate_limiter import check_webhook_rate_limit
from backend.app.services.webhook import discover_tunnel_url, wait_for_dns

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 3


# ---------------------------------------------------------------------------
# Pydantic models for BlueBubbles webhook payloads
# ---------------------------------------------------------------------------


class BBHandle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    address: str = ""
    service: str = ""


class BBAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    guid: str = ""
    mime_type: str = Field(default="", alias="mimeType")
    transfer_name: str = Field(default="", alias="transferName")
    total_bytes: int = Field(default=0, alias="totalBytes")


class BBChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    guid: str = ""


class BBMessageData(BaseModel):
    guid: str = ""
    text: str | None = None
    is_from_me: bool = Field(default=False, alias="isFromMe")
    handle: BBHandle | None = None
    attachments: list[BBAttachment] = []
    chats: list[BBChat] = []
    is_audio_message: bool = Field(default=False, alias="isAudioMessage")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class BBWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = ""
    data: BBMessageData | None = None


# ---------------------------------------------------------------------------
# Webhook auto-registration
# ---------------------------------------------------------------------------


async def register_bluebubbles_webhook(server_url: str, webhook_url: str) -> bool:
    """Register a webhook subscription with the BlueBubbles server.

    Calls ``POST /api/v1/webhook`` to register *webhook_url* for
    ``new-message`` events. Returns ``True`` on success.
    """
    url = f"{server_url}/api/v1/webhook"
    payload = {
        "url": webhook_url,
        "events": ["new-message"],
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=payload,
                params={"password": settings.bluebubbles_password},
                timeout=settings.http_timeout_seconds,
            )
            if resp.status_code >= 400:
                logger.error(
                    "BlueBubbles webhook registration failed: %s %s",
                    resp.status_code,
                    resp.text,
                )
                return False

            # Log without the password query param
            safe_url = webhook_url.split("?")[0]
            logger.info("BlueBubbles webhook registered: %s", safe_url)
            return True
    except httpx.HTTPError:
        logger.exception("Failed to register BlueBubbles webhook")
        return False


# ---------------------------------------------------------------------------
# BlueBubbles channel implementation
# ---------------------------------------------------------------------------


class BlueBubblesChannel(BaseChannel):
    """BlueBubbles implementation combining inbound webhooks and outbound sending."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # In-memory cache: sender_address -> chat_guid
        self._chat_cache: dict[str, str] = {}

    @property
    def _http(self) -> httpx.AsyncClient:
        """Lazily create the httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.bluebubbles_server_url,
                timeout=settings.http_timeout_seconds,
            )
        return self._client

    # -- BaseChannel identity --------------------------------------------------

    @property
    def name(self) -> str:
        return "bluebubbles"

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Discover tunnel URL and auto-register BlueBubbles webhook."""
        if not settings.bluebubbles_server_url or not settings.bluebubbles_password:
            return

        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        tunnel_url = await discover_tunnel_url()
        if not tunnel_url:
            logger.debug(
                "Cloudflare tunnel not detected: skipping BlueBubbles webhook auto-registration"
            )
            return

        webhook_url = (
            f"{tunnel_url}/api/webhooks/bluebubbles?password={settings.bluebubbles_password}"
        )

        if not await wait_for_dns(tunnel_url):
            logger.warning(
                "Tunnel hostname never became resolvable: skipping BlueBubbles webhook registration"
            )
            return

        ok = await register_bluebubbles_webhook(settings.bluebubbles_server_url, webhook_url)
        if ok:
            logger.info(
                "BlueBubbles webhook auto-registered: %s",
                webhook_url.split("?")[0],
            )
        else:
            logger.warning("Failed to auto-register BlueBubbles webhook")

    async def stop(self) -> None:
        """Close the httpx client on shutdown."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Inbound ---------------------------------------------------------------

    def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return True if the sender passes the BlueBubbles allowlist.

        In premium mode, approval is based on whether a ``ChannelRoute``
        exists for this sender. In OSS mode, ``sender_id`` (phone number
        or email) is checked against ``settings.bluebubbles_allowed_numbers``:
        empty denies all, ``"*"`` allows all, or a specific value must match.
        """
        premium = self._check_premium_route(sender_id)
        if premium is not None:
            return premium

        allowed = settings.bluebubbles_allowed_numbers.strip()
        if not allowed:
            return False
        if allowed == "*":
            return True
        return sender_id == allowed

    @staticmethod
    def parse_webhook(payload: BBWebhookPayload) -> InboundMessage | None:
        """Parse a BlueBubbles webhook payload into an InboundMessage.

        Returns None if the payload should be ignored.
        """
        if payload.type != "new-message":
            return None

        data = payload.data
        if not data:
            return None

        if data.is_from_me:
            return None

        handle = data.handle
        if not handle or not handle.address:
            logger.warning("BlueBubbles message missing handle address, ignoring")
            return None

        text = data.text or ""
        media_refs: list[tuple[str, str]] = [
            (att.guid, att.mime_type or "application/octet-stream")
            for att in data.attachments
            if att.guid
        ]

        external_id = f"bb_{data.guid}" if data.guid else ""

        return InboundMessage(
            channel="bluebubbles",
            sender_id=handle.address,
            text=text,
            media_refs=media_refs,
            external_message_id=external_id,
            sender_username=None,
        )

    def get_router(self) -> APIRouter:
        """Build a router with the BlueBubbles webhook endpoint."""
        router = APIRouter()
        channel = self

        @router.post("/webhooks/bluebubbles")
        async def bluebubbles_inbound(
            request: Request,
            _rate_limit: None = Depends(check_webhook_rate_limit),
        ) -> JSONResponse:
            """Receive inbound messages from BlueBubbles."""
            # Validate password query param
            password = request.query_params.get("password", "")
            if settings.bluebubbles_password and not hmac.compare_digest(
                password, settings.bluebubbles_password
            ):
                logger.warning("Invalid BlueBubbles webhook password")
                return JSONResponse(content={"ok": True})

            try:
                raw: dict = await request.json()
            except ValueError:
                logger.warning("BlueBubbles webhook received invalid JSON")
                return JSONResponse(content={"ok": True})

            logger.debug("BlueBubbles webhook raw payload: %s", raw)

            try:
                payload = BBWebhookPayload.model_validate(raw)
            except Exception:
                logger.warning("BlueBubbles webhook payload failed validation")
                return JSONResponse(content={"ok": True})

            data = payload.data
            logger.debug(
                "BlueBubbles webhook parsed: type=%s isFromMe=%s handle=%s attachments=%d",
                payload.type,
                data.is_from_me if data else "",
                data.handle.address if data and data.handle else "",
                len(data.attachments) if data else 0,
            )

            inbound = BlueBubblesChannel.parse_webhook(payload)
            if inbound is None:
                logger.debug("BlueBubbles parse_webhook returned None, skipping")
                return JSONResponse(content={"ok": True})

            if not channel.is_allowed(inbound.sender_id, ""):
                logger.info("Sender %s not in BlueBubbles allowlist, ignoring", inbound.sender_id)
                return JSONResponse(content={"ok": True})

            # Cache the chat_guid for outbound use
            if data and data.chats and data.chats[0].guid and data.handle:
                channel._chat_cache[data.handle.address] = data.chats[0].guid

            # Idempotency: skip duplicate messages
            if inbound.external_message_id:
                idempotency = get_idempotency_store()
                if idempotency.has_seen(inbound.external_message_id):
                    logger.info(
                        "Duplicate BlueBubbles webhook for %s, skipping",
                        inbound.external_message_id,
                    )
                    return JSONResponse(content={"ok": True})
                await idempotency.mark_seen(inbound.external_message_id)

            await message_bus.publish_inbound(inbound)
            return JSONResponse(content={"ok": True})

        return router

    # -- Outbound --------------------------------------------------------------

    def _get_chat_guid(self, to: str) -> str:
        """Return the cached chat GUID, or construct one from the address."""
        cached = self._chat_cache.get(to)
        if cached:
            return cached
        return f"iMessage;-;{to}"

    async def send_text(self, to: str, body: str) -> str:
        """Send a text message via BlueBubbles API."""
        chat_guid = self._get_chat_guid(to)
        resp = await self._http.post(
            "/api/v1/message/text",
            json={"chatGuid": chat_guid, "message": body},
            params={"password": settings.bluebubbles_password},
        )
        if resp.status_code >= 400:
            logger.error("BlueBubbles send_text failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        return data.get("guid", "")

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send a message with a media attachment via BlueBubbles API.

        Downloads the media from the URL, then uploads it as multipart form data.
        """
        chat_guid = self._get_chat_guid(to)

        # Download the media first
        async with httpx.AsyncClient() as dl_client:
            dl_resp = await dl_client.get(media_url, timeout=settings.http_timeout_seconds)
            dl_resp.raise_for_status()
            media_content = dl_resp.content
            content_type = dl_resp.headers.get("content-type", "application/octet-stream")

        filename = generate_filename(content_type.split(";")[0])

        files = {"attachment": (filename, media_content, content_type)}
        data_fields = {"chatGuid": chat_guid}
        if body:
            data_fields["message"] = body

        resp = await self._http.post(
            "/api/v1/message/attachment",
            data=data_fields,
            files=files,
            params={"password": settings.bluebubbles_password},
        )
        if resp.status_code >= 400:
            logger.error("BlueBubbles send_media failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        result = resp.json()
        return result.get("guid", "")

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send a text or media message."""
        if not media_urls:
            return await self.send_text(to, body)
        # Send the first media with the body text, then any additional media without body
        result = await self.send_media(to, body, media_urls[0])
        for url in media_urls[1:]:
            await self.send_media(to, "", url)
        return result

    async def send_typing_indicator(self, to: str) -> None:
        """Send a typing indicator via BlueBubbles API (best-effort)."""
        chat_guid = self._get_chat_guid(to)
        try:
            await self._http.post(
                f"/api/v1/chat/{chat_guid}/typing",
                params={"password": settings.bluebubbles_password},
            )
        except Exception:
            logger.debug("Failed to send BlueBubbles typing indicator to %s", to)

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media by BlueBubbles attachment GUID."""
        resp = await self._http.get(
            f"/api/v1/attachment/{file_id}/download",
            params={"password": settings.bluebubbles_password},
            timeout=settings.http_timeout_seconds,
        )
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
