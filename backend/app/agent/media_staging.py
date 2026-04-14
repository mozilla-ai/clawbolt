"""In-memory staging cache for inbound media bytes.

Holds downloaded media content keyed by (user_id, original_url) for a short
TTL so tools like ``upload_to_storage`` can find the bytes even when the
agent calls them on a turn after the attachment arrived. Scoped per-user
and per-process; not durable.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

STAGING_TTL_SECONDS = 3600


_cache: dict[str, dict[str, tuple[bytes, str, float]]] = {}


def stage(user_id: str, original_url: str, content: bytes, mime_type: str) -> None:
    """Cache media bytes for later retrieval within the TTL window."""
    if not original_url or not content:
        return
    expires_at = time.monotonic() + STAGING_TTL_SECONDS
    _cache.setdefault(user_id, {})[original_url] = (content, mime_type, expires_at)
    _purge_expired()


def get_all_for_user(user_id: str) -> dict[str, bytes]:
    """Return non-expired staged bytes for a user as ``{original_url: bytes}``."""
    _purge_expired()
    now = time.monotonic()
    return {
        url: content for url, (content, _mime, exp) in _cache.get(user_id, {}).items() if exp > now
    }


def evict(user_id: str, original_url: str) -> None:
    """Remove a staged entry (call after successful upload or explicit deny)."""
    user_items = _cache.get(user_id)
    if user_items:
        user_items.pop(original_url, None)
        if not user_items:
            _cache.pop(user_id, None)


def clear_user(user_id: str) -> None:
    """Drop all staged media for a user (primarily for tests)."""
    _cache.pop(user_id, None)


def _purge_expired() -> None:
    now = time.monotonic()
    empty_users: list[str] = []
    for user_id, items in _cache.items():
        expired = [url for url, (_c, _m, exp) in items.items() if exp <= now]
        for url in expired:
            del items[url]
        if not items:
            empty_users.append(user_id)
    for user_id in empty_users:
        del _cache[user_id]
