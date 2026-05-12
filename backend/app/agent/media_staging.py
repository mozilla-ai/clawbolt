"""In-memory staging cache for inbound media bytes.

Holds downloaded media content keyed by ``(user_id, original_url)`` for a
TTL window so agent tools (``analyze_photo``, ``upload_to_storage``,
``discard_media``, etc.) can find the bytes across turns. Scoped per-user
and per-process; not durable.

Each staged entry gets a short handle token (``media_XXXXXX``) so tools
can reference the bytes without passing raw channel URLs through the
prompt. Both lookup styles work; the handle-based API is what the agent
sees.

This module also tracks a short-lived ``recently_uploaded`` record for each
``(user_id, original_url)``. After a tool successfully ships the bytes to
an external service (CompanyCam, etc.) it both ``evict``s the bytes and
``mark_uploaded``s an ``UploadRecord`` so a same-turn retry on the same
handle can return an idempotent "already uploaded" result instead of the
generic "No photo available" error.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STAGING_TTL_SECONDS = 86400  # 24h: long agent sessions can span multiple hours
STAGING_MAX_PER_USER = 50  # Cap memory growth: oldest-expiring entry is evicted on overflow

# Recently-uploaded receipt cache. Shorter TTL than staging because its job
# is only to absorb same-turn / same-day retries on a handle the model
# already shipped. Anything older than this should not look like a fresh
# success on retry; the tool can return its normal NOT_FOUND.
UPLOAD_RECORD_TTL_SECONDS = 3600  # 1h
UPLOAD_RECORD_MAX_PER_USER = 200


@dataclass(frozen=True)
class UploadRecord:
    """A receipt of a successful external upload, scoped to a (user, handle).

    Returned by :func:`get_uploaded` so a tool that finds its staged bytes
    already evicted can re-emit the same external receipt instead of an
    error. Tools should populate ``service`` with a short identifier
    (e.g. ``"companycam"``) so a future cross-service handle reuse cannot
    surface the wrong receipt.
    """

    service: str
    external_id: str
    url: str
    target: str
    status: str  # "uploaded", "duplicate", "pending", "processing_error"


_cache: dict[str, dict[str, tuple[bytes, str, float, str]]] = {}
# handle token -> (user_id, original_url) reverse index
_handles: dict[str, tuple[str, str]] = {}
# user_id -> {original_url: (UploadRecord, expires_at)} recently-uploaded receipts
_uploaded: dict[str, dict[str, tuple[UploadRecord, float]]] = {}


def _mint_handle() -> str:
    """Generate a short opaque handle token for a staged media item.

    Collisions on 48 bits of entropy are astronomically unlikely, but a
    retry loop is free insurance against silent cross-user overwrite of
    the ``_handles`` index.
    """
    while True:
        handle = f"media_{secrets.token_urlsafe(6)}"
        if handle not in _handles:
            return handle


def stage(user_id: str, original_url: str, content: bytes, mime_type: str) -> str | None:
    """Cache media bytes for later retrieval within the TTL window.

    Returns the handle token for the staged entry, or ``None`` when staging
    was skipped (empty url or empty content). Safe to call repeatedly for
    the same ``original_url``, the handle is stable across re-stage within
    the same user's scope.
    """
    if not original_url or not content:
        return None
    # Purge first so re-stage of an expired URL doesn't resurrect a stale
    # handle that may no longer be indexed in _handles.
    _purge_expired()
    expires_at = time.monotonic() + STAGING_TTL_SECONDS
    user_items = _cache.setdefault(user_id, {})
    existing = user_items.get(original_url)
    if existing is not None:
        handle = existing[3]
    else:
        handle = _mint_handle()
        _handles[handle] = (user_id, original_url)
    user_items[original_url] = (content, mime_type, expires_at, handle)
    _enforce_per_user_cap(user_id)
    return handle


def _enforce_per_user_cap(user_id: str) -> None:
    """Evict the soonest-expiring entry when a user exceeds the per-user cap.

    Prevents unbounded memory growth when a single contractor sends hundreds
    of photos within the TTL window. The eviction is silent at the API level
    but logs a warning.
    """
    user_items = _cache.get(user_id)
    if not user_items or len(user_items) <= STAGING_MAX_PER_USER:
        return
    # Drop entries with the smallest expires_at until within cap.
    while len(user_items) > STAGING_MAX_PER_USER:
        oldest_url = min(user_items, key=lambda url: user_items[url][2])
        _content, _mime, _exp, handle = user_items.pop(oldest_url)
        _handles.pop(handle, None)
        logger.warning(
            "media_staging cap reached for user %s, evicted %s (handle=%s)",
            user_id,
            oldest_url,
            handle,
        )


def get_all_for_user(user_id: str) -> dict[str, bytes]:
    """Return non-expired staged bytes for a user as ``{original_url: bytes}``."""
    _purge_expired()
    now = time.monotonic()
    return {
        url: content
        for url, (content, _mime, exp, _handle) in _cache.get(user_id, {}).items()
        if exp > now
    }


def get_mime_type(user_id: str, original_url: str) -> str | None:
    """Return the staged mime type for ``original_url``, or None if not cached.

    The download step knows the authoritative mime type; the LLM is guessing.
    ``upload_to_storage`` uses this to override its argument when available.
    """
    _purge_expired()
    now = time.monotonic()
    entry = _cache.get(user_id, {}).get(original_url)
    if entry is None:
        return None
    _content, mime, exp, _handle = entry
    return mime if exp > now else None


def get_handle_for(user_id: str, original_url: str) -> str | None:
    """Return the staged handle for ``(user_id, original_url)`` or ``None``."""
    _purge_expired()
    entry = _cache.get(user_id, {}).get(original_url)
    if entry is None:
        return None
    _content, _mime, exp, handle = entry
    return handle if exp > time.monotonic() else None


def get_by_handle(handle: str) -> tuple[str, str, bytes, str] | None:
    """Look up a staged entry by its handle token.

    Returns ``(user_id, original_url, content, mime_type)`` or ``None`` if
    the handle is unknown or the entry has expired.
    """
    _purge_expired()
    ref = _handles.get(handle)
    if ref is None:
        return None
    user_id, original_url = ref
    entry = _cache.get(user_id, {}).get(original_url)
    if entry is None:
        _handles.pop(handle, None)
        return None
    content, mime, exp, stored_handle = entry
    now = time.monotonic()
    if exp <= now or stored_handle != handle:
        return None
    return user_id, original_url, content, mime


def touch(handle: str) -> bool:
    """Extend the TTL on a staged entry because a tool referenced it.

    Long agent sessions span multiple back-and-forth turns; touching on
    every tool reference prevents a stale-TTL eviction mid-conversation.
    Returns True if the handle was found and its TTL was extended.
    """
    ref = _handles.get(handle)
    if ref is None:
        return False
    user_id, original_url = ref
    entry = _cache.get(user_id, {}).get(original_url)
    if entry is None:
        return False
    content, mime, _old_exp, stored_handle = entry
    if stored_handle != handle:
        return False
    new_exp = time.monotonic() + STAGING_TTL_SECONDS
    _cache[user_id][original_url] = (content, mime, new_exp, stored_handle)
    return True


def resolve_media_ref(user_id: str, ref: str) -> tuple[str, bytes, str] | None:
    """Resolve a media reference that may be a handle or an original URL.

    The LLM sees media handles (``media_XXXXXX``) in the prompt and may
    pass them to tools that expect original URLs. This function accepts
    either form and returns ``(original_url, content, mime_type)`` when
    the referenced media is still staged for the given user.

    Returns ``None`` when the reference cannot be resolved (unknown handle,
    wrong user, expired entry, or URL not in staging).
    """
    # Try handle resolution first (handles start with "media_" but so could
    # a theoretical URL, so always check the handle index regardless).
    entry = get_by_handle(ref)
    if entry is not None:
        stored_uid, original_url, content, mime = entry
        if stored_uid == user_id:
            return original_url, content, mime
        return None

    # Fall back to URL lookup in the user's staged entries.
    _purge_expired()
    now = time.monotonic()
    user_items = _cache.get(user_id, {})
    item = user_items.get(ref)
    if item is not None:
        content, mime, exp, _handle = item
        if exp > now:
            return ref, content, mime

    return None


def evict(user_id: str, original_url: str) -> None:
    """Remove a staged entry (call after successful upload or explicit deny).

    The upload-record cache is intentionally NOT cleared here; a same-turn
    retry on the same handle still needs to find the receipt. Records age
    out via their own TTL.
    """
    user_items = _cache.get(user_id)
    if not user_items:
        return
    entry = user_items.pop(original_url, None)
    if entry is not None:
        _handles.pop(entry[3], None)
    if not user_items:
        _cache.pop(user_id, None)


def mark_uploaded(
    user_id: str,
    original_url: str,
    *,
    service: str,
    external_id: str,
    url: str,
    target: str,
    status: str,
) -> None:
    """Record that ``original_url`` was successfully shipped to *service*.

    Call right before :func:`evict` on the success path of an external
    upload tool. A later tool call referencing the same handle (within
    :data:`UPLOAD_RECORD_TTL_SECONDS`) can then look up the record via
    :func:`get_uploaded` and return an idempotent receipt instead of
    failing with "No photo available."
    """
    if not original_url:
        return
    _purge_expired_uploads()
    expires_at = time.monotonic() + UPLOAD_RECORD_TTL_SECONDS
    record = UploadRecord(
        service=service,
        external_id=external_id,
        url=url,
        target=target,
        status=status,
    )
    user_records = _uploaded.setdefault(user_id, {})
    user_records[original_url] = (record, expires_at)
    _enforce_upload_record_cap(user_id)


def get_uploaded(user_id: str, original_url: str) -> UploadRecord | None:
    """Return the recently-uploaded receipt for ``(user_id, original_url)``.

    Returns ``None`` if no upload was recorded or the record has expired.
    The returned record is unscoped by service, so callers must check
    ``record.service`` matches the tool's own service before re-emitting
    the receipt.
    """
    if not original_url:
        return None
    user_records = _uploaded.get(user_id)
    if not user_records:
        return None
    entry = user_records.get(original_url)
    if entry is None:
        return None
    record, expires_at = entry
    if expires_at <= time.monotonic():
        # Don't bother purging here; _purge_expired_uploads runs on writes.
        return None
    return record


def _enforce_upload_record_cap(user_id: str) -> None:
    """Cap the per-user upload-record count by evicting the soonest-expiring."""
    user_records = _uploaded.get(user_id)
    if not user_records or len(user_records) <= UPLOAD_RECORD_MAX_PER_USER:
        return
    while len(user_records) > UPLOAD_RECORD_MAX_PER_USER:
        oldest_url = min(user_records, key=lambda url: user_records[url][1])
        user_records.pop(oldest_url, None)


def _purge_expired_uploads() -> None:
    now = time.monotonic()
    empty_users: list[str] = []
    for user_id, records in _uploaded.items():
        expired = [url for url, (_rec, exp) in records.items() if exp <= now]
        for url in expired:
            del records[url]
        if not records:
            empty_users.append(user_id)
    for user_id in empty_users:
        del _uploaded[user_id]


def evict_by_handle(handle: str) -> bool:
    """Remove a staged entry by its handle. Returns True if something was removed."""
    ref = _handles.pop(handle, None)
    if ref is None:
        return False
    user_id, original_url = ref
    user_items = _cache.get(user_id)
    if user_items is not None:
        user_items.pop(original_url, None)
        if not user_items:
            _cache.pop(user_id, None)
    return True


def clear_user(user_id: str) -> None:
    """Drop all staged media and upload records for a user (primarily for tests)."""
    user_items = _cache.pop(user_id, None)
    if user_items:
        for _content, _mime, _exp, handle in user_items.values():
            _handles.pop(handle, None)
    _uploaded.pop(user_id, None)


def _purge_expired() -> None:
    now = time.monotonic()
    empty_users: list[str] = []
    for user_id, items in _cache.items():
        expired_urls: list[str] = []
        for url, (_c, _m, exp, handle) in items.items():
            if exp <= now:
                expired_urls.append(url)
                _handles.pop(handle, None)
        for url in expired_urls:
            del items[url]
        if not items:
            empty_users.append(user_id)
    for user_id in empty_users:
        del _cache[user_id]
