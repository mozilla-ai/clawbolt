"""Reply helper for messages from senders not in the allowlist.

When an inbound message arrives from a phone or chat ID that isn't connected
to a Clawbolt account, we send a one-time templated reply explaining the
situation and pointing them at sign-up. Rate-limited per (channel, sender_id)
so we can't be turned into a spam relay.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from backend.app.config import settings
from backend.app.logging_utils import mask_pii

if TYPE_CHECKING:
    from backend.app.channels.base import BaseChannel

logger = logging.getLogger(__name__)


_recent_replies: dict[tuple[str, str], float] = {}


def _build_reply_body() -> str:
    url = settings.unknown_sender_signup_url.strip()
    if url:
        return f"Hi! This number isn't connected to a Clawbolt account. Sign up at {url}."
    return (
        "Hi! This number isn't connected to a Clawbolt account. "
        "Visit clawbolt.ai to sign up, or ask the person who invited you to add you."
    )


def _claim_reply_slot(channel_name: str, sender_id: str, *, now: float | None = None) -> bool:
    """Reserve a reply slot for (channel, sender_id) if cooldown has elapsed.

    Updates the timestamp eagerly: a transient send failure won't trigger another
    reply attempt until the cooldown elapses. This is the conservative choice for
    a spam-relay guard.
    """
    if now is None:
        now = time.monotonic()
    cooldown = settings.unknown_sender_reply_cooldown_seconds
    key = (channel_name, sender_id)
    last = _recent_replies.get(key)
    if last is not None and now - last < cooldown:
        return False
    _recent_replies[key] = now
    return True


def reset_unknown_sender_cache() -> None:
    """Test helper: clear the in-memory rate-limit cache."""
    _recent_replies.clear()


async def reply_to_unknown_sender(channel: BaseChannel, sender_id: str) -> bool:
    """Send a one-shot 'not connected' reply to *sender_id* via *channel*.

    Returns True if a reply was attempted (sent or raised), False if it was
    suppressed by the rate limiter or by an empty sender_id.
    """
    if not sender_id:
        return False
    if not _claim_reply_slot(channel.name, sender_id):
        logger.debug(
            "%s: unknown sender %s already notified within cooldown, skipping",
            channel.name,
            mask_pii(sender_id),
        )
        return False

    body = _build_reply_body()
    try:
        await channel.send_text(sender_id, body)
    except Exception:
        logger.exception(
            "%s: failed to send unknown-sender reply to %s",
            channel.name,
            mask_pii(sender_id),
        )
        return True

    logger.info(
        "%s: sent unknown-sender reply to %s",
        channel.name,
        mask_pii(sender_id),
    )
    return True
