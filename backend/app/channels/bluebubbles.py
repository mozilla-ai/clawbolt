"""BlueBubbles channel: inbound webhook + outbound messaging (iMessage via self-hosted Mac bridge)."""

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from backend.app.agent.ingestion import InboundMessage
from backend.app.channels.base import BaseChannel, handle_webhook_inbound
from backend.app.config import settings
from backend.app.database import get_async_engine
from backend.app.logging_utils import mask_pii
from backend.app.media.download import DownloadedMedia, download_bounded, generate_filename
from backend.app.services.rate_limiter import check_webhook_rate_limit
from backend.app.services.webhook import discover_tunnel_url, wait_for_dns

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 3

# Postgres advisory lock key for the startup backfill, mirroring the
# pattern in ``inbound_recovery._RECOVERY_LOCK_KEY``. ``hashtext`` reduces
# the string to an int the lock function accepts.
_BACKFILL_LOCK_KEY = "bluebubbles_backfill:cleanup"

# Cap on messages returned from a single backfill query. The BlueBubbles
# server itself caps at 1000; 200 is well above what a real outage produces
# (a heavy iMessage user gets maybe 30 messages in 30 minutes) and bounds
# the worst-case replay backlog.
_BACKFILL_QUERY_LIMIT = 200

# Per-attempt timeout for the backfill HTTP call. Generous enough for a
# slow Mac to walk its message DB; bounded so a wedged server cannot
# stretch startup indefinitely. Falls back to ``http_timeout_seconds`` if
# that is shorter.
_BACKFILL_TIMEOUT_SECONDS = 30.0

# Typing indicators run as fire-and-forget tasks (see ChannelManager).
# Bound them tightly so even if the BlueBubbles server is wedged the task
# clears quickly instead of consuming an HTTP slot for the full default
# request timeout.
_TYPING_TIMEOUT_SECONDS = 3.0

# Outbound send retry policy. Self-hosted BlueBubbles servers running on
# consumer hardware frequently hit transient failures (Mac wakes from
# sleep, brief WiFi blip, BlueBubbles process restart). Without retry,
# the dispatcher catches the exception and silently drops the reply.
#
# Per-attempt timeout is shorter than the default ``http_timeout_seconds``
# (30s) so a hung server does not stretch the worst case past ~50s. With
# 1 initial attempt + 3 retries and 1s/2s/4s backoff that totals
# 10+1+10+2+10+4+10 = 47s before giving up.
_SEND_TIMEOUT_SECONDS = 10.0
_SEND_RETRY_BACKOFFS = (1.0, 2.0, 4.0)
_TRANSIENT_HTTP_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


# Path the BlueBubbles server POSTs inbound messages to, and the single event
# we subscribe to. Both the registration path and the health check that
# verifies registration read these, so the two can never drift apart.
INBOUND_WEBHOOK_PATH = "/api/webhooks/bluebubbles"
INBOUND_WEBHOOK_EVENT = "new-message"

# Timeout for the server-info probe. Deliberately short: this runs on a timer
# against a residential Mac, and a slow answer is itself a bad sign.
_SERVER_INFO_TIMEOUT_SECONDS = 5.0


def _derive_webhook_token(password: str) -> str:
    """Derive a webhook authentication token from the BlueBubbles server password.

    The raw password is used for API calls to the BlueBubbles server, but the
    webhook callback URL uses this derived token instead so the actual password
    never appears in request URLs or server access logs.
    """
    return hmac.new(
        key=b"clawbolt-bluebubbles-webhook-token",
        msg=password.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


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


@dataclass(frozen=True)
class RegisteredWebhook:
    """One webhook subscription as the BlueBubbles server reports it."""

    id: str
    url: str
    # Empty when the server's event list could not be parsed. Callers must
    # treat that as "unknown", never as "not subscribed": guessing wrong
    # would report a working bridge as broken.
    events: tuple[str, ...] = ()


def _parse_webhook_events(raw: object) -> tuple[str, ...]:
    """Normalize the ``events`` field, which BlueBubbles has shipped three ways.

    Depending on server version it arrives as a JSON-encoded string, a list of
    plain strings, or a list of ``{"label": ..., "value": ...}`` objects.
    Anything unrecognized yields an empty tuple, meaning "unknown".
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return (raw,) if raw else ()
    if not isinstance(raw, list):
        return ()
    events: list[str] = []
    for item in raw:
        if isinstance(item, str):
            events.append(item)
        elif isinstance(item, dict):
            entry = cast("dict[str, object]", item)
            value = entry.get("value") or entry.get("label")
            if isinstance(value, str):
                events.append(value)
    return tuple(events)


async def list_bluebubbles_webhooks(
    server_url: str, password: str = ""
) -> list[RegisteredWebhook] | None:
    """List webhook subscriptions registered on the BlueBubbles server.

    Returns ``None`` when the list could not be retrieved at all, which is
    distinct from an empty list: "the server told us there are no webhooks"
    is an actionable finding, "we could not ask" is not.

    Catches broadly on purpose. This feeds a health check and a cleanup pass,
    and both want "unknown" rather than an exception when a third-party server
    answers with something unexpected.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{server_url}/api/v1/webhook",
                params={"password": password or settings.bluebubbles_password},
                timeout=settings.http_timeout_seconds,
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
    except Exception:
        logger.debug("Could not list BlueBubbles webhooks", exc_info=True)
        return None

    raw_hooks = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(raw_hooks, list):
        return None

    hooks: list[RegisteredWebhook] = []
    for wh in raw_hooks:
        if not isinstance(wh, dict):
            continue
        wh_id = wh.get("id")
        wh_url = wh.get("url")
        if wh_id is None or not isinstance(wh_url, str):
            continue
        hooks.append(
            RegisteredWebhook(
                id=str(wh_id), url=wh_url, events=_parse_webhook_events(wh.get("events"))
            )
        )
    return hooks


def _webhook_matches(candidate_url: str, expected_url: str) -> bool:
    """True when a registered webhook URL is the one we would register now.

    Compares scheme, host, path, and the ``token`` query parameter rather than
    the raw strings, so a server that re-encodes the query on read-back still
    matches. The token is part of the comparison on purpose: a registration
    carrying a token derived from an old password is delivered to us and then
    rejected at the door, which looks identical to no registration at all.
    """
    candidate, expected = urlparse(candidate_url), urlparse(expected_url)
    if (candidate.scheme, candidate.netloc, candidate.path.rstrip("/")) != (
        expected.scheme,
        expected.netloc,
        expected.path.rstrip("/"),
    ):
        return False
    return parse_qs(candidate.query).get("token") == parse_qs(expected.query).get("token")


async def _cleanup_stale_webhooks(server_url: str, our_endpoint: str) -> None:
    """Remove existing BlueBubbles webhooks that point to our endpoint.

    BlueBubbles accumulates webhook registrations on each startup.  This
    lists all registered webhooks and deletes any whose URL contains our
    endpoint path, preventing duplicate deliveries.
    """
    webhooks = await list_bluebubbles_webhooks(server_url)
    if not webhooks:
        return
    try:
        async with httpx.AsyncClient() as client:
            for wh in webhooks:
                if our_endpoint not in wh.url:
                    continue
                await client.delete(
                    f"{server_url}/api/v1/webhook/{wh.id}",
                    params={"password": settings.bluebubbles_password},
                    timeout=settings.http_timeout_seconds,
                )
                logger.info("Removed stale BlueBubbles webhook %s", wh.id)
    except Exception:
        logger.debug("Could not clean up stale BlueBubbles webhooks", exc_info=True)


async def register_bluebubbles_webhook(server_url: str, webhook_url: str) -> bool:
    """Register a webhook subscription with the BlueBubbles server.

    Cleans up any existing webhooks for our endpoint first to prevent
    duplicate deliveries, then calls ``POST /api/v1/webhook`` to register
    *webhook_url* for ``new-message`` events.  Returns ``True`` on success.
    """
    # Remove stale registrations before adding the new one
    endpoint = webhook_url.split("?")[0]
    await _cleanup_stale_webhooks(server_url, endpoint)

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

            # Log without the token query param
            logger.info("BlueBubbles webhook registered: %s", endpoint)
            return True
    except httpx.ConnectError as exc:
        logger.warning("BlueBubbles server not reachable at %s: %s", server_url, exc)
        return False
    except httpx.HTTPError:
        logger.exception("Failed to register BlueBubbles webhook")
        return False


def build_webhook_url(base_url: str, password: str = "") -> str:
    """Build the inbound webhook URL to register for *base_url*.

    Single definition shared by the code that registers the webhook and the
    health check that verifies it is still registered. If these were built
    separately, a change to either would make the check quietly compare
    against a URL nobody registers.
    """
    token = _derive_webhook_token(password or settings.bluebubbles_password)
    return f"{base_url.rstrip('/')}{INBOUND_WEBHOOK_PATH}?token={quote(token, safe='')}"


@dataclass(frozen=True)
class WebhookCheck:
    """Whether the BlueBubbles server will actually deliver inbound messages."""

    ok: bool
    detail: str = ""
    # False when the webhook list could not be retrieved, so ``ok=False`` means
    # "unknown" rather than "missing" and callers should not try to repair it.
    listed: bool = False
    registered_endpoints: tuple[str, ...] = ()


async def verify_webhook_registration(
    server_url: str, expected_webhook_url: str, password: str = ""
) -> WebhookCheck:
    """Check that *expected_webhook_url* is registered for new-message events.

    This closes the gap that nothing else covers. Registration happens once at
    startup; if the Mac was asleep or slow at that moment the attempt fails,
    logs a warning, and is never retried. The bridge then comes back online,
    every reachability check goes green, and inbound iMessage stays dead until
    somebody redeploys. The same silence follows a base-URL change, which
    leaves the old registration pointing at a URL that no longer answers.
    """
    webhooks = await list_bluebubbles_webhooks(server_url, password)
    if webhooks is None:
        return WebhookCheck(
            ok=False,
            detail="could not list webhooks on the BlueBubbles server",
            listed=False,
        )

    endpoints = tuple(sorted({wh.url.split("?")[0] for wh in webhooks}))
    expected_endpoint = expected_webhook_url.split("?")[0]

    for wh in webhooks:
        if not _webhook_matches(wh.url, expected_webhook_url):
            continue
        # An empty event tuple means the server's format was unrecognized.
        # Unknown is not a failure.
        if wh.events and INBOUND_WEBHOOK_EVENT not in wh.events:
            return WebhookCheck(
                ok=False,
                detail=(
                    f"webhook is registered but subscribed to {', '.join(wh.events)} "
                    f"rather than {INBOUND_WEBHOOK_EVENT}"
                ),
                listed=True,
                registered_endpoints=endpoints,
            )
        return WebhookCheck(ok=True, listed=True, registered_endpoints=endpoints)

    if any(endpoint == expected_endpoint for endpoint in endpoints):
        detail = (
            f"a webhook for {expected_endpoint} is registered but its token does not "
            "match the current server password, so deliveries are rejected on arrival"
        )
    elif endpoints:
        detail = (
            f"no webhook registered for {expected_endpoint}; the server has "
            f"{len(webhooks)} registered instead: {', '.join(endpoints)}"
        )
    else:
        detail = (
            f"no webhooks are registered on the BlueBubbles server (expected {expected_endpoint})"
        )
    return WebhookCheck(ok=False, detail=detail, listed=True, registered_endpoints=endpoints)


# ---------------------------------------------------------------------------
# Server health probing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlueBubblesHealth:
    """Outcome of one ``/api/v1/server/info`` probe.

    ``reachable`` and ``authenticated`` are separate fields because they fail
    for different reasons and need different fixes: an unreachable host is a
    sleeping Mac or a dead tunnel and may resolve itself, while a rejected
    password is a configuration error that waiting will never fix. A single
    ``status_code < 500`` boolean could not tell them apart and reported an
    HTTP 401 as a healthy bridge.

    The readiness flags are tri-state. ``None`` means the server did not report
    that field, which older BlueBubbles builds do; a missing field must never
    manufacture an outage.
    """

    reachable: bool
    authenticated: bool
    detail: str = ""
    server_version: str = ""
    private_api: bool | None = None
    helper_connected: bool | None = None
    # Whether the Mac is signed in to iMessage. Stored as a bool rather than
    # the account address the server reports, so the operator's iCloud email
    # never reaches a log line, an alert email, or the admin UI.
    imessage_signed_in: bool | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and self.authenticated


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


async def probe_bluebubbles_server(
    server_url: str,
    password: str,
    timeout: float = _SERVER_INFO_TIMEOUT_SECONDS,
) -> BlueBubblesHealth:
    """Probe ``/api/v1/server/info`` and report what the answer proves.

    Never raises: an exception here is itself the health signal.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{server_url}/api/v1/server/info",
                params={"password": password},
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        # The exception text can carry the request URL, and the URL carries
        # the password. Report the type only.
        return BlueBubblesHealth(
            reachable=False,
            authenticated=False,
            detail=f"{type(exc).__name__} contacting the BlueBubbles server",
        )

    if resp.status_code in (401, 403):
        return BlueBubblesHealth(
            reachable=True,
            authenticated=False,
            detail=(
                f"BlueBubbles rejected the server password (HTTP {resp.status_code}); "
                "check BLUEBUBBLES_PASSWORD"
            ),
        )
    if resp.status_code >= 500:
        return BlueBubblesHealth(
            reachable=False,
            authenticated=False,
            detail=f"BlueBubbles server returned HTTP {resp.status_code}",
        )
    if resp.status_code != 200:
        return BlueBubblesHealth(
            reachable=True,
            authenticated=False,
            detail=(
                f"unexpected HTTP {resp.status_code} from /api/v1/server/info; "
                "check BLUEBUBBLES_SERVER_URL points at a BlueBubbles server"
            ),
        )

    try:
        body = resp.json()
    except ValueError:
        return BlueBubblesHealth(
            reachable=True,
            authenticated=False,
            detail="/api/v1/server/info returned a non-JSON body",
        )
    payload = body.get("data") if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        payload = {}

    account = payload.get("detected_icloud")
    return BlueBubblesHealth(
        reachable=True,
        authenticated=True,
        server_version=str(payload.get("server_version") or ""),
        private_api=_optional_bool(payload.get("private_api")),
        helper_connected=_optional_bool(payload.get("helper_connected")),
        imessage_signed_in=bool(account) if "detected_icloud" in payload else None,
    )


def describe_send_readiness(health: BlueBubblesHealth, send_method: str = "") -> str:
    """Return why outbound iMessage would fail, or ``""`` when nothing is wrong.

    ``/api/v1/server/info`` answering proves the bridge process is alive. It
    does not prove the Mac can still send: Messages.app signed out of iMessage,
    or a private-api send method whose helper is not connected, both leave the
    server perfectly reachable and every send failing.
    """
    if health.imessage_signed_in is False:
        return "the Mac is not signed in to iMessage (BlueBubbles reports no iCloud account)"
    if (send_method or settings.bluebubbles_send_method) == "private-api":
        if health.private_api is False:
            return "send method is private-api but the BlueBubbles Private API is disabled"
        if health.helper_connected is False:
            return (
                "send method is private-api but the BlueBubbles Private API helper is not connected"
            )
    return ""


# ---------------------------------------------------------------------------
# BlueBubbles channel implementation
# ---------------------------------------------------------------------------


class BlueBubblesChannel(BaseChannel):
    """BlueBubbles implementation combining inbound webhooks and outbound sending."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # In-memory cache: sender_address -> chat_guid
        self._chat_cache: dict[str, str] = {}
        # Set to True once the BlueBubbles server is confirmed reachable and
        # answering authenticated requests.
        self.server_reachable: bool = False
        # Full result of the most recent probe, kept so monitoring can report
        # *why* the bridge is unhealthy and check send readiness without
        # running a second poller against the operator's Mac. ``None`` until
        # the first probe completes.
        self.last_health: BlueBubblesHealth | None = None
        # Background tasks owned by this channel: periodic health check
        # and recurring backfill. Started in ``start()`` and cancelled in
        # ``stop()`` so the lifespan shutdown is clean.
        self._health_task: asyncio.Task[None] | None = None
        self._backfill_task: asyncio.Task[None] | None = None

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

    async def check_health(self) -> BlueBubblesHealth:
        """Probe the server, remember the result, and return it."""
        health = await probe_bluebubbles_server(
            settings.bluebubbles_server_url, settings.bluebubbles_password
        )
        self.last_health = health
        return health

    async def _check_server_reachable(self) -> bool:
        """Ping the BlueBubbles server to verify connectivity.

        "Reachable" requires an authenticated 200, not merely a non-5xx.
        A wrong password answers 401 while delivering nothing, and treating
        that as reachable put a green light on a bridge that could not pass a
        single message.
        """
        return (await self.check_health()).ok

    async def start(self) -> None:
        """Discover tunnel URL and auto-register BlueBubbles webhook."""
        if not settings.bluebubbles_server_url or not settings.bluebubbles_password:
            return

        await asyncio.sleep(STARTUP_DELAY_SECONDS)

        # Verify the server is actually reachable before advertising as
        # configured. Do this regardless of who registered the inbound
        # webhook: on premium the PaaS lifespan registers a webhook on the
        # BlueBubbles server, but that doesn't tell us whether the user's
        # Mac is actually up and reachable; the dashboard still needs the
        # reachability signal so it doesn't gray out a working channel.
        self.server_reachable = await self._check_server_reachable()
        # Start the periodic health + backfill loops regardless of the
        # boot-time reachability result. A Mac that is asleep right now
        # may wake up in five minutes, and we want the loops to detect
        # that without requiring a deploy.
        self._start_background_tasks()
        if not self.server_reachable:
            logger.warning(
                "BlueBubbles server not reachable at %s",
                settings.bluebubbles_server_url,
            )
            return

        # If a PaaS webhook was already registered (e.g. via premium on
        # Railway), skip the tunnel discovery retry loop entirely.
        if self.webhook_registered:
            return

        tunnel_url = await discover_tunnel_url()
        if not tunnel_url:
            logger.debug(
                "Cloudflare tunnel not detected: skipping BlueBubbles webhook auto-registration"
            )
            return

        webhook_url = build_webhook_url(tunnel_url)

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

    def _start_background_tasks(self) -> None:
        """Spawn the periodic health and backfill loops.

        Both loops are best-effort: they swallow per-iteration exceptions
        so a transient BB-server failure does not kill the loop. The
        tasks are stored on the instance so ``stop()`` can cancel them
        cleanly on shutdown. Each loop is gated by its own interval
        setting and skips itself entirely when the interval is 0.
        """
        if self._health_task is None and settings.bluebubbles_health_check_interval_seconds > 0:
            self._health_task = asyncio.create_task(self._health_loop())
        if self._backfill_task is None and settings.bluebubbles_backfill_interval_seconds > 0:
            self._backfill_task = asyncio.create_task(self._backfill_loop())

    async def _health_loop(self) -> None:
        """Re-poll ``/api/v1/server/info`` so reachability stays current.

        At deploy boot we set ``server_reachable`` once and the dashboard
        keeps showing that result until the next restart. That hides
        intermittent failures: when the basement Mac sleeps or restarts,
        every tenant on that BlueBubbles server goes silent and the
        dashboard light stays green for hours until someone notices.
        Running the same probe on a timer surfaces the failure within
        ``bluebubbles_health_check_interval_seconds``.
        """
        interval = settings.bluebubbles_health_check_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    health = await self.check_health()
                except Exception:
                    logger.exception("BlueBubbles periodic health check failed")
                    continue
                if health.ok != self.server_reachable:
                    if health.ok:
                        logger.info(
                            "BlueBubbles server healthy again at %s",
                            settings.bluebubbles_server_url,
                        )
                    else:
                        logger.warning(
                            "BlueBubbles server went unhealthy at %s: %s",
                            settings.bluebubbles_server_url,
                            health.detail,
                        )
                    self.server_reachable = health.ok
        except asyncio.CancelledError:
            return

    async def _backfill_loop(self) -> None:
        """Re-run ``run_startup_backfill`` on a timer.

        BlueBubbles' webhook delivery is fire-and-forget with no retry,
        so a webhook lost mid-flight (transient receiver hiccup, lambda
        cold start past the BB request timeout, brief network blip)
        leaves the message stranded on the Mac. Without a recurring
        sweep, that message only surfaces on the next restart. The boot
        path is unchanged; this loop is purely additive.
        """
        interval = settings.bluebubbles_backfill_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.run_startup_backfill()
                except Exception:
                    logger.exception("BlueBubbles periodic backfill failed")
        except asyncio.CancelledError:
            return

    async def register_paas_webhook(self, base_url: str) -> bool | None:
        """Register BlueBubbles webhook using a stable PaaS base URL."""
        if not settings.bluebubbles_server_url or not settings.bluebubbles_password:
            return None
        webhook_url = build_webhook_url(base_url)
        return await register_bluebubbles_webhook(settings.bluebubbles_server_url, webhook_url)

    async def stop(self) -> None:
        """Close the httpx client and cancel background tasks on shutdown."""
        for task in (self._health_task, self._backfill_task):
            if task is not None and not task.done():
                task.cancel()
        self._health_task = None
        self._backfill_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Inbound ---------------------------------------------------------------

    async def is_allowed(self, sender_id: str, username: str) -> bool:
        """Return True if the sender passes the BlueBubbles allowlist.

        In premium mode, approval is based on whether a ``ChannelRoute``
        exists for this sender. In OSS mode, ``sender_id`` (phone number
        or email) is checked against ``settings.bluebubbles_allowed_numbers``:
        empty denies all, ``"*"`` allows all, or a specific value must match.
        """
        return await self._check_static_allowlist(settings.bluebubbles_allowed_numbers, sender_id)

    @staticmethod
    def parse_webhook(payload: BBWebhookPayload) -> InboundMessage | None:
        """Parse a BlueBubbles webhook payload into an InboundMessage.

        Returns ``None`` if the payload should be ignored.

        Accepts both ``new-message`` and ``updated-message`` events.
        BlueBubbles re-fires the webhook for the same iMessage GUID
        whenever a tracked state field on the message row changes
        (``dateDelivered``, ``isDelivered``, ``dateRead``,
        ``dateEdited``, ``dateRetracted``, ``didNotifyRecipient``,
        ``hasUnsentParts`` -- see ``getMessageEvent`` in the BB
        server's ``pollers/index.ts``). Each emission serializes the
        message row's CURRENT joined state, including its current
        attachment list. For an inbound iMessage that ships text
        before its attachments are joined into the ``chat.db`` row,
        the first ``new-message`` may carry zero attachments and a
        subsequent ``updated-message`` (typically the
        ``dateDelivered`` transition) carries the now-complete list.
        Dropping ``updated-message`` silently loses those attachments.
        Note: attachment count is *not* itself a tracked delta, so a
        message that gains attachments with no other state change
        will never trigger a follow-up event; the periodic backfill
        loop is the safety net for that case.

        Both event types are kept under distinct
        ``external_message_id`` values (``bb_<guid>`` vs
        ``bb_<guid>_att``) so each gets its own idempotency row and
        rides the bus as a separate inbound. ``MessageBatcher``
        (default window 1500 ms) coalesces the two into one pipeline
        dispatch and merges media from both. ``updated-message``
        events that bring no attachments return ``None`` -- their
        only payload would be a delivered/read tick the agent has
        nothing to do with.
        """
        if payload.type not in ("new-message", "updated-message"):
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

        # Ignore ``updated-message`` events that bring no new attachment
        # information. BlueBubbles fires ``updated-entry`` on every
        # delivered/read/edited delta; only the attachment-arrival flavor
        # is worth processing for our pipeline. The first webhook for a
        # text+image message has zero attachments; the follow-up has the
        # attachments. So ``len(media_refs) > 0`` is a sufficient
        # discriminator without parsing event subtype heuristics.
        if payload.type == "updated-message":
            if not media_refs:
                return None
            # The original ``new-message`` already persisted the text
            # body; suppress it on the follow-up so the batcher doesn't
            # see two copies of the caption when it coalesces.
            text = ""

        # ``new-message`` uses the bare GUID; ``updated-message`` suffixes
        # ``_att`` so the two events get distinct idempotency rows. They
        # are coalesced downstream by ``MessageBatcher`` (whose default
        # 1.5 s window comfortably covers the ~350 ms BB-server gap),
        # which merges media from all batched entries before dispatch.
        external_id = ""
        if data.guid:
            external_id = (
                f"bb_{data.guid}" if payload.type == "new-message" else f"bb_{data.guid}_att"
            )

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
            # Validate webhook token.  Accept either the derived ?token= or
            # the raw ?password= (BlueBubbles appends the password to webhook
            # URLs by default, so stale registrations use that form).
            token = request.query_params.get("token", "")
            if not token:
                raw_pw = request.query_params.get("password", "")
                if raw_pw:
                    token = _derive_webhook_token(raw_pw)
            expected = _derive_webhook_token(settings.bluebubbles_password)
            if settings.bluebubbles_password and not hmac.compare_digest(token, expected):
                logger.warning("Invalid BlueBubbles webhook token")
                return JSONResponse(content={"ok": True})

            try:
                raw: dict = await request.json()
            except ValueError:
                logger.warning("BlueBubbles webhook received invalid JSON")
                return JSONResponse(content={"ok": True})

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
                mask_pii(data.handle.address) if data and data.handle else "",
                len(data.attachments) if data else 0,
            )

            inbound = BlueBubblesChannel.parse_webhook(payload)

            def _cache_chat_guid() -> None:
                if data and data.chats and data.chats[0].guid and inbound is not None:
                    channel._chat_cache[inbound.sender_id] = data.chats[0].guid

            return await handle_webhook_inbound(
                channel,
                inbound,
                on_accepted=_cache_chat_guid,
            )

        return router

    # -- Outbound --------------------------------------------------------------

    def _get_chat_guid(self, to: str) -> str:
        """Return the cached chat GUID, or construct one from the address."""
        cached = self._chat_cache.get(to)
        if cached:
            return cached
        return f"iMessage;-;{to}"

    async def _post_with_retry(self, url: str, **kwargs: object) -> httpx.Response:
        """POST to BlueBubbles with bounded retries on transient failures.

        Retries on connection errors, read/write/pool timeouts, remote
        protocol errors, and 5xx responses. Does not retry on 4xx (those
        indicate caller error: bad payload, missing chat, auth failure)
        or non-network exceptions. Retries are safe because BlueBubbles
        dedupes on ``tempGuid`` which the caller passes through unchanged.
        """
        delays: tuple[float, ...] = (0.0, *_SEND_RETRY_BACKOFFS)
        last_exc: Exception | None = None
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await self._http.post(url, timeout=_SEND_TIMEOUT_SECONDS, **kwargs)  # type: ignore[arg-type]
            except _TRANSIENT_HTTP_EXCEPTIONS as exc:
                last_exc = exc
                logger.warning(
                    "BlueBubbles %s transient failure (attempt %d/%d): %s",
                    url,
                    attempt + 1,
                    len(delays),
                    type(exc).__name__,
                )
                continue
            if resp.status_code < 500:
                return resp
            # 5xx: server error, worth retrying
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code} from {url}", request=resp.request, response=resp
            )
            logger.warning(
                "BlueBubbles %s 5xx (attempt %d/%d): status=%d",
                url,
                attempt + 1,
                len(delays),
                resp.status_code,
            )
        assert last_exc is not None
        raise last_exc

    async def send_text(self, to: str, body: str) -> str:
        """Send a text message via BlueBubbles API."""
        chat_guid = self._get_chat_guid(to)
        payload = {
            "chatGuid": chat_guid,
            "message": body,
            "tempGuid": f"temp-{uuid.uuid4()}",
            "method": settings.bluebubbles_send_method,
        }
        logger.info(
            "BlueBubbles send_text: to=%s chatGuid=%s method=%s bodyLen=%d",
            mask_pii(to),
            mask_pii(chat_guid),
            settings.bluebubbles_send_method,
            len(body),
        )
        resp = await self._post_with_retry(
            "/api/v1/message/text",
            json=payload,
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
        data_fields = {
            "chatGuid": chat_guid,
            "tempGuid": f"temp-{uuid.uuid4()}",
            "method": settings.bluebubbles_send_method,
        }
        if body:
            data_fields["message"] = body

        resp = await self._post_with_retry(
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

    async def send_typing_indicator(self, to: str) -> None:
        """Send a typing indicator via BlueBubbles API (best-effort).

        Requires the BlueBubbles Private API to be enabled on the server.
        Silently skipped when using apple-script send method.
        """
        if settings.bluebubbles_send_method != "private-api":
            return
        chat_guid = self._get_chat_guid(to)
        try:
            resp = await self._http.post(
                f"/api/v1/chat/{chat_guid}/typing",
                params={"password": settings.bluebubbles_password},
                timeout=_TYPING_TIMEOUT_SECONDS,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "BlueBubbles typing indicator non-200: status=%s chatGuid=%s body=%s",
                    resp.status_code,
                    mask_pii(chat_guid),
                    resp.text[:500],
                )
        except Exception:
            logger.exception("Failed to send BlueBubbles typing indicator to %s", mask_pii(to))

    async def stop_typing_indicator(self, to: str) -> None:
        """Clear an active typing indicator via BlueBubbles API (best-effort).

        iMessage typing indicators do not expire promptly on their own, so
        when the agent decides not to reply we explicitly cancel the
        indicator to avoid a phantom "typing..." with no follow-up message.
        Skipped in apple-script mode where typing indicators are never sent.
        """
        if settings.bluebubbles_send_method != "private-api":
            return
        chat_guid = self._get_chat_guid(to)
        try:
            resp = await self._http.delete(
                f"/api/v1/chat/{chat_guid}/typing",
                params={"password": settings.bluebubbles_password},
                timeout=_TYPING_TIMEOUT_SECONDS,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "BlueBubbles stop typing non-200: status=%s chatGuid=%s body=%s",
                    resp.status_code,
                    mask_pii(chat_guid),
                    resp.text[:500],
                )
        except Exception:
            logger.exception("Failed to stop BlueBubbles typing indicator for %s", mask_pii(to))

    async def download_media(self, file_id: str) -> DownloadedMedia:
        """Download media by BlueBubbles attachment GUID.

        Streams with a hard size cap and wall-time deadline so a slow or
        oversized attachment can't OOM or stall the worker.
        """
        content, headers = await download_bounded(
            self._http,
            f"/api/v1/attachment/{file_id}/download",
            params={"password": settings.bluebubbles_password},
        )
        content_type = headers.get("content-type", "application/octet-stream").split(";")[0]

        filename = generate_filename(content_type)
        return DownloadedMedia(
            content=content,
            mime_type=content_type,
            original_url=file_id,
            filename=filename,
        )

    # -- Startup backfill ------------------------------------------------------

    async def run_startup_backfill(self) -> int:
        """Replay BlueBubbles messages received during a Clawbolt outage.

        The webhook from BlueBubbles to ``POST /api/webhooks/bluebubbles`` is
        fire-and-forget. If Clawbolt was hung when the webhook fired, the
        message never reached our DB. ``recover_orphan_inbound_messages``
        only handles messages that landed in the DB and then crashed the
        in-memory pipeline, so it cannot help here.

        On startup we ask the BlueBubbles server for any messages dated in
        the last ``settings.bluebubbles_backfill_lookback_minutes`` and run
        each through ``handle_webhook_inbound``. The idempotency store
        rejects anything we already processed via the live webhook, so this
        is safe to run on every boot, including healthy ones, where it
        becomes a no-op after dedup.

        Returns the number of messages we attempted to replay (before
        idempotency dedup), mostly for the startup log line. Best-effort:
        swallows query failures so a wedged BlueBubbles server cannot
        block startup.
        """
        if not settings.bluebubbles_server_url or not settings.bluebubbles_password:
            return 0

        lookback_minutes = settings.bluebubbles_backfill_lookback_minutes
        if lookback_minutes == 0:
            logger.debug("BlueBubbles backfill disabled (bluebubbles_backfill_lookback_minutes=0)")
            return 0

        # The advisory lock is session-scoped (lives on the underlying
        # PG connection until released or the connection closes). We
        # must hold it on a dedicated ``AsyncConnection`` rather than an
        # ``AsyncSession``: ``AsyncSession.commit()`` returns the
        # connection to the pool, where a peer coroutine can check it
        # out and call ``pg_try_advisory_lock`` on it (locks are
        # reentrant per PG session), letting both callers enter the
        # critical section. The replay business logic still uses its
        # own ``AsyncSession`` via ``handle_webhook_inbound``; that
        # session is on a SEPARATE connection from the lock, which is
        # fine because the lock lives on the pinned ``lock_conn``.
        lock_conn = await get_async_engine().connect()
        lock_acquired = False
        try:
            if not await _try_acquire_backfill_lock(lock_conn):
                logger.info("Another worker is running BlueBubbles backfill; skipping on this boot")
                return 0
            lock_acquired = True

            cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                minutes=lookback_minutes
            )
            cutoff_ms = int(cutoff.timestamp() * 1000)

            try:
                resp = await self._http.post(
                    "/api/v1/message/query",
                    params={"password": settings.bluebubbles_password},
                    json={
                        "with": ["chat", "handle", "attachment"],
                        "after": cutoff_ms,
                        "sort": "ASC",
                        "limit": _BACKFILL_QUERY_LIMIT,
                    },
                    timeout=_BACKFILL_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError:
                logger.warning(
                    "BlueBubbles backfill query failed (server unreachable or slow)",
                    exc_info=True,
                )
                return 0

            if resp.status_code >= 400:
                logger.warning(
                    "BlueBubbles backfill query returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return 0

            try:
                body = resp.json()
            except ValueError:
                logger.warning("BlueBubbles backfill query returned non-JSON body")
                return 0

            raw_messages = body.get("data") or []
            if not raw_messages:
                return 0

            attempted = 0
            for raw in raw_messages:
                try:
                    data = BBMessageData.model_validate(raw)
                except Exception:
                    logger.debug("BlueBubbles backfill: skipping malformed message")
                    continue

                payload = BBWebhookPayload(type="new-message", data=data)
                inbound = BlueBubblesChannel.parse_webhook(payload)
                if inbound is None:
                    continue

                # Mirror the live webhook's chat-cache population so any
                # outbound replies the agent produces can find the chat
                # GUID without reconstructing it from sender_id.
                def _cache_chat_guid(
                    d: BBMessageData = data, sender: str = inbound.sender_id
                ) -> None:
                    if d.chats and d.chats[0].guid:
                        self._chat_cache[sender] = d.chats[0].guid

                try:
                    await handle_webhook_inbound(self, inbound, on_accepted=_cache_chat_guid)
                    attempted += 1
                except Exception:
                    logger.exception(
                        "BlueBubbles backfill: handle_webhook_inbound failed for extId=%s",
                        inbound.external_message_id,
                    )

            if attempted:
                logger.info(
                    "BlueBubbles backfill: replayed %d message(s) since %s "
                    "(idempotency dedups already-seen)",
                    attempted,
                    cutoff.isoformat(),
                )
            return attempted
        finally:
            if lock_acquired:
                await _release_backfill_lock(lock_conn)
            await lock_conn.close()


# ---------------------------------------------------------------------------
# Startup backfill helpers
# ---------------------------------------------------------------------------
#
# The backfill itself is a method on ``BlueBubblesChannel`` (see
# ``run_startup_backfill``); these advisory-lock helpers live at module
# level because they mirror the shape of ``inbound_recovery._try_acquire_lock``
# and have no per-instance state.


async def _try_acquire_backfill_lock(conn: AsyncConnection) -> bool:
    """Acquire the per-process backfill advisory lock.

    ``pg_try_advisory_lock`` returns True on first acquisition, False if
    another worker on a rolling restart already holds the lock. The
    advisory lock is session-scoped: it lives on the underlying PG
    connection until released or the connection closes.

    ``conn`` MUST be an ``AsyncConnection`` (not an ``AsyncSession``).
    ``AsyncSession.commit()`` returns the underlying DBAPI connection
    to the pool, where a peer coroutine can check it out and call
    ``pg_try_advisory_lock`` on it; PG advisory locks are reentrant
    per session, so both callers would enter the critical section. The
    advisory lock must stay pinned to the same physical connection
    from acquire through unlock; only ``AsyncConnection`` provides
    that pinning.

    The caller MUST hold the same ``AsyncConnection`` across acquire +
    critical section + release; otherwise ``pg_advisory_unlock`` runs
    on a different connection and silently no-ops (see
    ``tests/test_inbound_recovery.py``,
    ``test_unlock_on_different_connection_is_a_no_op``).

    No commit is issued here: ``Connection.execute()`` runs in an
    implicit transaction that is fine to leave open; the advisory
    lock takes effect immediately and persists until the connection
    closes. Committing would not return the connection to the pool
    (we hold it directly), so it's safe to skip.
    """
    try:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"),
            {"k": _BACKFILL_LOCK_KEY},
        )
        got = result.scalar()
    except Exception:
        logger.exception("Failed to acquire BlueBubbles backfill advisory lock")
        return False
    return bool(got)


async def _release_backfill_lock(conn: AsyncConnection) -> None:
    """Best-effort release of the backfill lock.

    ``conn`` MUST be the same ``AsyncConnection`` that ``_try_acquire_backfill_lock``
    succeeded on. See that function's docstring for the connection-pinning
    rationale: ``pg_advisory_unlock`` on a different connection is a
    silent no-op, so passing an ``AsyncSession`` (whose ``commit`` may
    have recycled the connection) lets the lock leak past the
    critical section.
    """
    try:
        await conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:k))"),
            {"k": _BACKFILL_LOCK_KEY},
        )
    except Exception:
        logger.exception("Failed to release BlueBubbles backfill advisory lock")
