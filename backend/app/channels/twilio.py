"""Twilio channel: inbound webhook + outbound messaging (SMS/MMS)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

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

# Hostnames we will issue authenticated media downloads against. Twilio's
# media URLs all live on these two subdomains; anything else is a
# misconfiguration or an attacker trying to exfiltrate the account-SID
# Basic Auth header via SSRF. The check defends the off-prod path where
# ``twilio_validate_signatures`` is disabled (otherwise signature
# validation already ensures we only fetch what Twilio actually sent).
_TWILIO_MEDIA_HOST_SUFFIXES = (".twilio.com", ".twiliocdn.com")


# ---------------------------------------------------------------------------
# Premium hooks
# ---------------------------------------------------------------------------

# Premium deployments provision one Twilio number per user. Two hooks plug
# into per-user behavior:
#
#   FromResolver:  maps an outbound recipient phone back to that user's
#                  own Twilio number so ``send_text``/``send_media`` can
#                  pin ``from_`` correctly.
#   ToVerifier:    on inbound, confirms that the webhook's ``(From, To)``
#                  pair corresponds to a user's own provisioned Twilio
#                  number. Without this, a user texting the wrong Twilio
#                  number (e.g. typo, stale contact) would still route to
#                  their own bot via the ``From``-based allowlist, but the
#                  reply would come from a different number than the one
#                  they texted — confusing UX. OSS leaves both unset and
#                  the channel behaves as one global bot.
FromResolver = Callable[[str], Awaitable[str | None]]
ToVerifier = Callable[[str, str], Awaitable[bool]]

_from_resolver: FromResolver | None = None
_to_verifier: ToVerifier | None = None


def set_twilio_from_resolver(fn: FromResolver) -> None:
    """Register a callable that returns the per-user ``from_`` for an outbound recipient.

    Called by the premium plugin during startup. ``fn`` receives the
    recipient phone and returns the user's provisioned Twilio number, or
    ``None`` to fall back to the global settings.
    """
    global _from_resolver
    _from_resolver = fn


def get_twilio_from_resolver() -> FromResolver | None:
    """Return the current per-user resolver, or ``None`` when unset."""
    return _from_resolver


def set_twilio_to_verifier(fn: ToVerifier) -> None:
    """Register a callable that confirms an inbound ``(From, To)`` pair is valid.

    Called by the premium plugin during startup. ``fn`` receives
    ``(from_phone, to_phone)`` and returns ``True`` if those identify a
    consistent user / provisioned-number pairing. When the verifier
    returns ``False`` or raises, the inbound webhook is dropped with a
    200 + empty TwiML (so probing senders can't infer state).
    """
    global _to_verifier
    _to_verifier = fn


def get_twilio_to_verifier() -> ToVerifier | None:
    """Return the current inbound ``(From, To)`` verifier, or ``None`` when unset."""
    return _to_verifier


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
        # default in our deployment). The debug log surfaces the exact
        # URL we're validating against so a deployment behind a proxy
        # that strips https can diagnose signature failures without
        # tcpdump.
        url = str(request.url)
        logger.debug("Twilio signature validation: url=%s", url)
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
        if not message_sid:
            # Real Twilio always sends MessageSid; an empty value here
            # means the payload came from a misconfigured proxy or a
            # spoofed request that slipped past signature validation
            # (only possible when validation is off). Without an SID we
            # can't dedupe Twilio's 24h retry loop, so log loud enough
            # for operators to notice and fix the upstream.
            logger.warning("Twilio webhook missing MessageSid; idempotency skipped")
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

            # Per-user deployments register a verifier that confirms the
            # webhook's ``(From, To)`` pair corresponds to one user's own
            # provisioned Twilio number. Dropping mismatches here keeps a
            # user from accidentally receiving a reply from a different
            # bot number than the one they texted. OSS leaves the
            # verifier unset and this check is a no-op.
            if inbound is not None and _to_verifier is not None:
                to_phone = form_data.get("To", "")
                try:
                    ok = await _to_verifier(inbound.sender_id, to_phone)
                except Exception:
                    logger.exception(
                        "Twilio to_verifier raised; dropping inbound to avoid misrouting",
                    )
                    return Response(content=TWIML_EMPTY, media_type="application/xml")
                if not ok:
                    logger.warning(
                        "Twilio inbound: (From, To) pair does not match a known user; dropping"
                    )
                    return Response(content=TWIML_EMPTY, media_type="application/xml")

            await handle_webhook_inbound(channel, inbound)
            return Response(content=TWIML_EMPTY, media_type="application/xml")

        return router

    # -- Outbound --------------------------------------------------------------

    @staticmethod
    async def _resolve_per_user_from(to: str) -> str | None:
        """Ask the premium resolver for the user's own Twilio number, if any."""
        resolver = _from_resolver
        if resolver is None:
            return None
        try:
            return await resolver(to)
        except Exception:
            # A resolver failure shouldn't block outbound delivery; fall
            # back to the global sender. The resolver itself is expected
            # to log its own failures.
            logger.exception("Twilio from_resolver raised; falling back to global sender")
            return None

    def _send_kwargs(self, to: str, body: str, from_override: str | None = None) -> dict[str, Any]:
        """Build kwargs for ``messages.create``.

        Precedence: ``from_override`` (per-user, premium) > Messaging
        Service SID (A2P 10DLC pool) > global ``twilio_phone_number``.
        Raises ``RuntimeError`` when none is available.
        """
        kwargs: dict[str, Any] = {"to": to, "body": body}
        if from_override:
            kwargs["from_"] = from_override
        elif settings.twilio_messaging_service_sid:
            kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
        elif settings.twilio_phone_number:
            kwargs["from_"] = settings.twilio_phone_number
        else:
            msg = (
                "Twilio outbound requires either TWILIO_MESSAGING_SERVICE_SID "
                "or TWILIO_PHONE_NUMBER to be set (or a registered per-user resolver)"
            )
            raise RuntimeError(msg)
        return kwargs

    async def send_text(self, to: str, body: str) -> str:
        """Send an SMS. *to* is an E.164 phone number. Returns the Twilio Message SID."""
        from_override = await self._resolve_per_user_from(to)
        kwargs = self._send_kwargs(to, body, from_override)
        message = await asyncio.to_thread(self.client.messages.create, **kwargs)
        return str(message.sid)

    async def send_media(self, to: str, body: str, media_url: str) -> str:
        """Send an MMS with one media attachment. Returns the Twilio Message SID."""
        from_override = await self._resolve_per_user_from(to)
        kwargs = self._send_kwargs(to, body, from_override)
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
        from_override = await self._resolve_per_user_from(to)
        kwargs = self._send_kwargs(to, body, from_override)
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

        Refuses any URL that doesn't live on a Twilio-owned host. This
        is defense in depth against an attacker who slipped a webhook
        past signature validation (only possible when validation is off
        for dev): without this guard, ``MediaUrl0=http://internal/...``
        would exfiltrate the account-SID Basic Auth header.
        """
        if not _is_twilio_host(file_id):
            msg = f"Refusing to download media from non-Twilio host: {file_id}"
            raise ValueError(msg)
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


def _is_twilio_host(url: str) -> bool:
    """Return True if *url*'s host is on the Twilio media domain.

    Returns False on malformed URLs and URLs with no host part (e.g. a
    bare path); callers should treat both as "not Twilio" and refuse.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _TWILIO_MEDIA_HOST_SUFFIXES)
