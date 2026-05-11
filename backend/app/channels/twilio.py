"""Twilio channel: inbound webhook + outbound messaging (SMS/MMS)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from backend.app.agent.ingestion import InboundMessage
from backend.app.channels.base import BaseChannel, handle_webhook_inbound
from backend.app.config import Settings, settings
from backend.app.media.download import DownloadedMedia, download_bounded, generate_filename
from backend.app.services.rate_limiter import check_webhook_rate_limit

logger = logging.getLogger(__name__)

TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"
# TwiML empty response. Twilio expects a 200 with valid TwiML; an empty
# <Response/> tells it "do nothing" so the agent loop, which delivers
# replies via the outbound dispatcher, isn't double-sending.
TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


class TwilioChannel(BaseChannel):
    """Twilio implementation combining inbound webhooks and outbound SMS/MMS sending."""

    def __init__(self, svc_settings: Settings | None = None) -> None:
        s = svc_settings or settings
        self._account_sid = s.twilio_account_sid
        self._auth_token = s.twilio_auth_token
        self._client: TwilioClient | None = None

    @property
    def client(self) -> TwilioClient:
        """Lazily create the Twilio REST client.

        Construction is cheap but errors on empty credentials, so we defer
        until the first send so an unconfigured deployment doesn't crash
        at import time.
        """
        if self._client is None:
            self._client = TwilioClient(self._account_sid, self._auth_token)
        return self._client

    # -- BaseChannel identity --------------------------------------------------

    @property
    def name(self) -> str:
        return "twilio"

    # -- Inbound ---------------------------------------------------------------

    @staticmethod
    def _validate_signature(request: Request, form_data: dict[str, str]) -> bool:
        """Validate Twilio's ``X-Twilio-Signature`` header.

        Returns ``False`` if validation is enabled and the signature does
        not match; callers should drop the request silently (200 + empty
        TwiML) so a probing attacker can't tell whether validation is on.
        """
        if not settings.twilio_validate_signatures:
            return True
        signature = request.headers.get(TWILIO_SIGNATURE_HEADER, "")
        validator = RequestValidator(settings.twilio_auth_token)
        # Twilio signs the exact URL the request arrived at, so reverse
        # proxies that rewrite the host or scheme will break validation
        # unless the public URL is reconstructed. ``request.url`` reflects
        # ASGI's view, which for our PaaS deployments matches the URL
        # Twilio saw because the proxy sets X-Forwarded-* headers and
        # Starlette honors them when ``proxy_headers=True`` (the uvicorn
        # default in our deployment).
        url = str(request.url)
        return validator.validate(url, form_data, signature)

    @staticmethod
    def _extract_media(form_data: dict[str, str]) -> list[tuple[str, str]]:
        """Pull media URLs + MIME types from Twilio's flat form payload."""
        try:
            num_media = int(form_data.get("NumMedia", "0"))
        except ValueError:
            return []
        media: list[tuple[str, str]] = []
        for i in range(num_media):
            url = form_data.get(f"MediaUrl{i}", "")
            content_type = form_data.get(f"MediaContentType{i}", "")
            if url:
                media.append((url, content_type or "application/octet-stream"))
        return media

    async def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return ``True`` if the sender passes the Twilio allowlist gate.

        In premium mode the ``ChannelRoute`` override resolves this. In
        OSS mode the static ``twilio_allowed_numbers`` setting applies:
        empty denies all, ``"*"`` allows all, otherwise the sender must
        match the configured E.164 number exactly.
        """
        return await self._check_static_allowlist(settings.twilio_allowed_numbers, sender_id)

    @staticmethod
    def parse_form(form_data: dict[str, str]) -> InboundMessage | None:
        """Parse a Twilio webhook form payload into an ``InboundMessage``.

        Returns ``None`` if the payload is missing the fields we need.
        """
        sender = form_data.get("From", "").strip()
        if not sender:
            logger.warning("Twilio webhook missing From, ignoring")
            return None

        body = form_data.get("Body", "")
        message_sid = form_data.get("MessageSid", "") or form_data.get("SmsMessageSid", "")
        external_id = f"twilio_{message_sid}" if message_sid else ""
        media = TwilioChannel._extract_media(form_data)

        return InboundMessage(
            channel="twilio",
            sender_id=sender,
            text=body,
            media_refs=media,
            external_message_id=external_id,
            sender_username=None,
        )

    def get_router(self) -> APIRouter:
        """Build a router with the Twilio webhook endpoint."""
        router = APIRouter()
        channel = self

        @router.post("/webhooks/twilio")
        async def twilio_inbound(
            request: Request,
            _rate_limit: None = Depends(check_webhook_rate_limit),
        ) -> Response:
            """Receive inbound SMS/MMS from Twilio.

            Returns an empty TwiML response immediately; the agent loop
            replies through the outbound dispatcher via the REST API so
            this endpoint stays under Twilio's 15s ack window even when
            the LLM call takes longer.
            """
            try:
                raw_form = await request.form()
            except Exception:
                logger.warning("Twilio webhook received unparseable form body")
                return Response(content=TWIML_EMPTY, media_type="application/xml")
            form_data = {k: str(v) for k, v in raw_form.items()}

            if not TwilioChannel._validate_signature(request, form_data):
                logger.warning("Invalid Twilio webhook signature")
                return Response(content=TWIML_EMPTY, media_type="application/xml")

            inbound = TwilioChannel.parse_form(form_data)
            await handle_webhook_inbound(channel, inbound)
            return Response(content=TWIML_EMPTY, media_type="application/xml")

        return router

    # -- Outbound --------------------------------------------------------------

    def _send_kwargs(self, to: str, body: str) -> dict[str, Any]:
        """Build kwargs for ``messages.create``.

        Pins ``messaging_service_sid`` when configured (the A2P 10DLC
        path) so Twilio picks an appropriate sender from the campaign
        pool; otherwise pins ``from_`` to the single configured number.
        """
        kwargs: dict[str, Any] = {"to": to, "body": body}
        if settings.twilio_messaging_service_sid:
            kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
        elif settings.twilio_phone_number:
            kwargs["from_"] = settings.twilio_phone_number
        else:
            msg = (
                "Twilio outbound requires either TWILIO_MESSAGING_SERVICE_SID "
                "or TWILIO_PHONE_NUMBER to be set"
            )
            raise RuntimeError(msg)
        return kwargs

    async def send_text(self, to: str, body: str) -> str:
        """Send an SMS. *to* is an E.164 phone number. Returns the Twilio Message SID."""
        kwargs = self._send_kwargs(to, body)
        message = await asyncio.to_thread(self.client.messages.create, **kwargs)
        return str(message.sid)

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send an MMS with one media attachment. Returns the Twilio Message SID."""
        kwargs = self._send_kwargs(to, body)
        kwargs["media_url"] = [media_url]
        message = await asyncio.to_thread(self.client.messages.create, **kwargs)
        return str(message.sid)

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send SMS or MMS in a single API call.

        Required override: Twilio accepts a list of media URLs on one
        ``messages.create`` call, so the multi-URL path should be one
        MMS (up to 10 media items) instead of N separate sends.
        """
        if not media_urls:
            return await self.send_text(to, body)
        kwargs = self._send_kwargs(to, body)
        kwargs["media_url"] = list(media_urls)
        message = await asyncio.to_thread(self.client.messages.create, **kwargs)
        return str(message.sid)

    async def send_typing_indicator(self, to: str) -> None:
        """Twilio SMS has no typing-indicator concept. No-op.

        Implemented to satisfy ``BaseChannel`` without sending the user
        misleading UX. Operators who need typing indicators should pick
        Linq (iMessage/RCS) or BlueBubbles instead.
        """
        return None

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media from a Twilio MediaUrl.

        For Twilio, ``file_id`` is the full ``MediaUrl{N}`` from the
        webhook form payload. The URL requires HTTP Basic Auth with the
        account SID + auth token. Streams with the standard hard size
        cap and wall-time deadline.
        """
        auth = httpx.BasicAuth(self._account_sid, self._auth_token)
        async with httpx.AsyncClient(auth=auth, timeout=settings.http_timeout_seconds) as client:
            content, headers = await download_bounded(client, file_id)
        content_type = headers.get("content-type", "application/octet-stream").split(";")[0]
        filename = generate_filename(content_type)
        return DownloadedMedia(
            content=content,
            mime_type=content_type,
            original_url=file_id,
            filename=filename,
        )
