"""Auto-discover Cloudflare Tunnel URL and register Telegram webhook."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

CLOUDFLARED_METRICS_URL = "http://tunnel:2000/quicktunnel"
TELEGRAM_API_BASE = "https://api.telegram.org"


async def discover_tunnel_url(
    max_retries: int = 10,
    delay: float = 2.0,
    metrics_url: str = CLOUDFLARED_METRICS_URL,
) -> str | None:
    """Poll cloudflared metrics API for the quick-tunnel hostname.

    Returns the public HTTPS URL or ``None`` if cloudflared is unreachable
    after *max_retries* attempts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(metrics_url, timeout=5.0)
                resp.raise_for_status()
                hostname = resp.json().get("hostname", "")
                if hostname:
                    return f"https://{hostname}"
        except (httpx.HTTPError, KeyError):
            pass
        if attempt < max_retries:
            logger.debug("cloudflared not ready (attempt %d/%d), retrying…", attempt, max_retries)
            await asyncio.sleep(delay)

    logger.debug("Cloudflare tunnel not found after %d attempts", max_retries)
    return None


async def register_telegram_webhook(
    bot_token: str,
    webhook_url: str,
    secret: str | None = None,
) -> bool:
    """Call Telegram ``setWebhook`` and return ``True`` on success."""
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/setWebhook"
    payload: dict[str, str] = {"url": webhook_url}
    if secret:
        payload["secret_token"] = secret

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook registered: %s", webhook_url)
                return True
            logger.error("Telegram setWebhook failed: %s", data)
            return False
    except httpx.HTTPError:
        logger.exception("Failed to register Telegram webhook")
        return False
