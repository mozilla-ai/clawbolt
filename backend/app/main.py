import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.config import settings
from backend.app.routers import auth, estimates, health, telegram_webhook
from backend.app.services.webhook import discover_tunnel_url, register_telegram_webhook

logger = logging.getLogger(__name__)


async def _auto_register_webhook() -> None:
    """Discover Cloudflare Tunnel URL and register Telegram webhook.

    Runs as a background task after the server is listening so that Telegram
    can reach the webhook URL during its validation check.
    """
    # Small delay to ensure Uvicorn is accepting connections.
    await asyncio.sleep(3)
    tunnel_url = await discover_tunnel_url()
    if tunnel_url:
        webhook_url = f"{tunnel_url}/api/webhooks/telegram"
        secret = settings.telegram_webhook_secret or None
        ok = await register_telegram_webhook(
            settings.telegram_bot_token, webhook_url, secret=secret
        )
        if ok:
            logger.info("Telegram webhook auto-registered: %s", webhook_url)
        else:
            logger.warning("Failed to auto-register Telegram webhook")
    else:
        logger.debug("Cloudflare tunnel not detected — skipping webhook auto-registration")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
    heartbeat_scheduler.start()

    # Fire-and-forget: register webhook after the server is ready.
    webhook_task: asyncio.Task[None] | None = None
    if settings.telegram_bot_token:
        webhook_task = asyncio.create_task(_auto_register_webhook())

    yield

    if webhook_task and not webhook_task.done():
        webhook_task.cancel()
    heartbeat_scheduler.stop()


app = FastAPI(title="Backshop", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(telegram_webhook.router, prefix="/api")
app.include_router(estimates.router, prefix="/api")
