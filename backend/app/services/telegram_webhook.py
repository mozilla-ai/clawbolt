"""Telegram webhook registration and bot info discovery for premium deployments.

On platforms like Railway where there is no Cloudflare tunnel, the OSS
auto-registration silently skips. This module provides:
1. Auto-registration at startup using APP_BASE_URL
2. Admin endpoint helpers for manual webhook management
3. Bot username discovery via getMe
"""

import logging

import httpx

from backend.app.config import TELEGRAM_API_BASE, get_effective_webhook_secret, settings
from backend.app.services.webhook import register_telegram_webhook

logger = logging.getLogger(__name__)

# Module-level cache for the bot username discovered at startup.
_bot_username: str = ""


def get_bot_username() -> str:
    """Return the cached bot username (empty string if unknown)."""
    return _bot_username


async def discover_bot_username() -> str | None:
    """Call Telegram getMe to discover the bot's username.

    Returns the username (without @) or None on failure.
    """
    global _bot_username
    token = settings.telegram_bot_token
    if not token:
        return None

    url = f"{TELEGRAM_API_BASE}/bot{token}/getMe"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            data = resp.json()
            if data.get("ok"):
                username = data["result"].get("username", "")
                if username:
                    _bot_username = username
                    logger.info("Discovered bot username via getMe: @%s", username)
                    return username
            logger.warning("Telegram getMe failed: %s", data)
    except httpx.HTTPError:
        logger.exception("Failed to call Telegram getMe")
    return None


async def register_webhook(webhook_url: str = "") -> tuple[bool, str]:
    """Register the Telegram webhook.

    If *webhook_url* is empty, constructs it from APP_BASE_URL.
    Delegates to the OSS ``register_telegram_webhook`` for the actual API call.
    Returns (success, webhook_url_used).
    """
    token = settings.telegram_bot_token
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set, skipping webhook registration")
        return False, ""

    if not webhook_url:
        base = settings.app_base_url.rstrip("/")
        webhook_url = f"{base}/api/webhooks/telegram"

    secret = get_effective_webhook_secret(settings) or None
    ok = await register_telegram_webhook(token, webhook_url, secret=secret)
    return ok, webhook_url


async def unregister_webhook() -> bool:
    """Remove the Telegram webhook (set to empty URL)."""
    token = settings.telegram_bot_token
    if not token:
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/setWebhook"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"url": ""}, timeout=10)
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook unregistered")
                return True
            logger.error("Telegram deleteWebhook failed: %s", data)
            return False
    except httpx.HTTPError:
        logger.exception("Failed to unregister Telegram webhook")
        return False
