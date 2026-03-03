import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.config import get_effective_webhook_secret, settings
from backend.app.routers import auth, estimates, health, telegram_webhook
from backend.app.services.webhook import (
    discover_tunnel_url,
    register_telegram_webhook,
    wait_for_dns,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
# Only the app's own loggers get the configured level; third-party libraries
# (httpcore, httpx, telegram, etc.) stay at WARNING to avoid noise.
logging.getLogger("backend").setLevel(settings.log_level.upper())
logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 3


async def _auto_register_webhook() -> None:
    """Discover Cloudflare Tunnel URL and register Telegram webhook.

    Runs as a background task after the server is listening so that Telegram
    can reach the webhook URL during its validation check.  Registration is
    retried several times because quick-tunnel hostnames are brand-new and
    Telegram's DNS may not resolve them immediately.
    """
    # Small delay to ensure Uvicorn is accepting connections.
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    tunnel_url = await discover_tunnel_url()
    if not tunnel_url:
        logger.debug("Cloudflare tunnel not detected — skipping webhook auto-registration")
        return

    webhook_url = f"{tunnel_url}/api/webhooks/telegram"
    secret = get_effective_webhook_secret(settings) or None

    # Wait for the quick-tunnel hostname to be DNS-resolvable before calling
    # setWebhook.  If we call too early, Telegram caches the negative DNS
    # response and all subsequent retries fail.
    if not await wait_for_dns(tunnel_url):
        logger.warning("Tunnel hostname never became resolvable — skipping webhook registration")
        return

    ok = await register_telegram_webhook(settings.telegram_bot_token, webhook_url, secret=secret)
    if ok:
        logger.info("Telegram webhook auto-registered: %s", webhook_url)
    else:
        logger.warning("Failed to auto-register Telegram webhook")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
    heartbeat_scheduler.start()

    if settings.telegram_bot_token:
        if settings.telegram_webhook_secret:
            logger.info("Webhook secret: using explicit TELEGRAM_WEBHOOK_SECRET")
        else:
            logger.info("Webhook secret: auto-derived from bot token")

    if (
        settings.telegram_bot_token
        and not settings.telegram_allowed_chat_ids
        and not settings.telegram_allowed_usernames
    ):
        logger.warning(
            "No Telegram allowlist configured (TELEGRAM_ALLOWED_CHAT_IDS / "
            "TELEGRAM_ALLOWED_USERNAMES). All messages will be rejected. "
            'Set to "*" to allow all users, or provide a comma-separated list of IDs/usernames.'
        )

    # Fire-and-forget: register webhook after the server is ready.
    webhook_task: asyncio.Task[None] | None = None
    if settings.telegram_bot_token:
        webhook_task = asyncio.create_task(_auto_register_webhook())

    yield

    if webhook_task and not webhook_task.done():
        webhook_task.cancel()
    heartbeat_scheduler.stop()


app = FastAPI(title="Clawbolt", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,  # type: ignore[arg-type]
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(telegram_webhook.router, prefix="/api")
app.include_router(estimates.router, prefix="/api")
