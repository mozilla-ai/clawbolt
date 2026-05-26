"""DB- and disk-backed staging cache for inbound media bytes.

Holds downloaded media content keyed by ``(user_id, original_url)`` for
a 7-day window so agent tools (``analyze_photo``, ``upload_to_storage``,
``discard_media``, etc.) can find the bytes across turns and across
process restarts. The bytes themselves live on the deployment's
persistent volume under ``settings.media_staging_base_dir``; metadata
(handle, original_url, mime, expiry, disk path) lives in the
``staged_media`` Postgres table.

Each staged entry gets a short handle token (``media_XXXXXX``) so tools
can reference the bytes without passing raw channel URLs through the
prompt. Both lookup styles work; the handle-based API is what the agent
sees.

This module also records an upload receipt on each row. After a tool
ships the bytes to an external service (CompanyCam, Drive, etc.) it
calls ``mark_uploaded`` to populate ``upload_*`` columns on the
matching ``staged_media`` row. A same-handle retry then surfaces an
idempotent receipt via ``get_uploaded`` instead of writing a second
copy. The receipt shares the row's TTL, so it survives worker
restarts and ages out alongside the bytes.

MULTI-REPLICA WARNING: this module assumes a single application
instance with exclusive write access to ``media_staging_base_dir``. If
clawbolt is ever deployed across multiple replicas, the on-disk bytes
are no longer shared and a replica that did not receive the inbound
message cannot find the file. Tracked at
https://github.com/mozilla-ai/clawbolt/issues/1336. When that work
lands, move ``content`` into Postgres ``BYTEA`` or an object store so
every replica can read it.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine.cursor import CursorResult

from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, db_session_async
from backend.app.models import StagedMedia

logger = logging.getLogger(__name__)

# 7 days: long enough that a contractor sending photos earlier in the
# week can still reference them mid-conversation later. Bytes survive
# process restarts via the persistent volume, so the cap is real wall
# clock, not "since the last deploy."
STAGING_TTL_SECONDS = 604800
STAGING_MAX_PER_USER = 50  # Cap memory growth: oldest-expiring entry is evicted on overflow


@dataclass(frozen=True)
class UploadRecord:
    """A receipt of a successful external upload, scoped to a staged handle.

    Returned by :func:`get_uploaded` so a tool with a same-handle retry
    can re-emit the same external receipt instead of writing a second
    copy. Tools populate ``service`` with a short identifier (e.g.
    ``"companycam"``) so a future cross-service handle reuse cannot
    surface the wrong receipt.

    Persisted as columns on the ``staged_media`` row that the handle
    points at, so the receipt shares the bytes' TTL and survives worker
    restarts.
    """

    service: str
    external_id: str
    url: str
    target: str
    status: str  # "uploaded", "duplicate", "pending", "processing_error"


def _staging_root() -> Path:
    """Return the configured staging directory, ensuring it exists."""
    root = Path(settings.media_staging_base_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user_id: str) -> Path:
    """Return ``<staging_root>/<user_id>``, ensuring it exists."""
    d = _staging_root() / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _disk_path_for(user_id: str, handle: str) -> Path:
    """Absolute filesystem path for ``(user, handle)``'s bytes.

    Used by callers that need to write or unlink the file. The DB stores
    the relative suffix (see :func:`_store_disk_path`) so the staging
    window survives an operator changing ``MEDIA_STAGING_BASE_DIR``.
    """
    return _user_dir(user_id) / f"{handle}.bin"


def _store_disk_path(user_id: str, handle: str) -> str:
    """Suffix stored in ``staged_media.disk_path``, relative to the staging root.

    Storing the suffix (e.g. ``<user_id>/media_abZtYWFs.bin``) rather than
    an absolute path means a deployment that re-mounts its volume under a
    different ``MEDIA_STAGING_BASE_DIR`` can still find its 7-day window of
    bytes after a restart.
    """
    return f"{user_id}/{handle}.bin"


def _resolve_disk_path(stored: str) -> Path:
    """Reconstruct an absolute path from a stored ``disk_path`` suffix.

    Defensively handles a legacy absolute path (e.g. from a pre-PR row
    or a hand-inserted test fixture): if the stored value is already
    absolute, return it as-is. New writes always use the relative form.
    """
    p = Path(stored)
    if p.is_absolute():
        return p
    return _staging_root() / stored


def _mint_handle() -> str:
    """Generate a short opaque handle token for a staged media item.

    48 bits of entropy: collision probability across a 10k-entry table
    is on the order of 1e-7 over the table's lifetime, so the INSERT
    either takes or raises ``uq_staged_media_handle`` and the
    exception propagates. ``stage`` does not catch it; a caller seeing
    the error should retry, which mints a fresh handle.
    """
    return f"media_{secrets.token_urlsafe(6)}"


def _unlink_quiet(path: Path) -> None:
    """Best-effort delete: log a warning on unexpected errors."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to unlink staged media file %s: %s", path, exc)


async def stage(user_id: str, original_url: str, content: bytes, mime_type: str) -> str | None:
    """Cache media bytes for later retrieval within the TTL window.

    Returns the handle token for the staged entry, or ``None`` when
    staging was skipped (empty url or empty content). Safe to call
    repeatedly for the same ``original_url``: the handle is stable
    across re-stage within the same user's scope.
    """
    if not original_url or not content:
        return None

    expires_at = datetime.now(UTC) + timedelta(seconds=STAGING_TTL_SECONDS)
    fresh_handle = _mint_handle()
    fresh_disk_path = _store_disk_path(user_id, fresh_handle)

    async with db_session_async() as db:
        # INSERT ... ON CONFLICT DO UPDATE: an existing row for
        # (user_id, original_url) keeps its handle and disk_path (so the
        # agent's prior reference still resolves) but refreshes mime,
        # expiry, and the underlying bytes. The RETURNING tells us which
        # disk path actually won the conflict so we write bytes there.
        stmt = (
            pg_insert(StagedMedia)
            .values(
                user_id=user_id,
                handle=fresh_handle,
                original_url=original_url,
                mime_type=mime_type,
                disk_path=fresh_disk_path,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "original_url"],
                set_={
                    "mime_type": mime_type,
                    "expires_at": expires_at,
                },
            )
            .returning(StagedMedia.handle, StagedMedia.disk_path)
        )
        row = (await db.execute(stmt)).one()
        await db.commit()
        handle = cast("str", row.handle)
        disk_path = _resolve_disk_path(cast("str", row.disk_path))

    # Write bytes after commit so a transient DB failure doesn't leave
    # an orphan file behind. A crash between commit and write leaves
    # an orphan row; the next ``get_*`` call detects the missing file
    # and prunes the row.
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(content)

    await _enforce_per_user_cap(user_id)
    return handle


async def _enforce_per_user_cap(user_id: str) -> None:
    """Evict soonest-expiring rows when a user exceeds the per-user cap.

    Prevents unbounded growth when a single contractor sends hundreds
    of photos within the TTL window. Eviction is silent at the API
    level but logs a warning so we notice if it happens routinely.

    Rows with a recorded ``upload_status`` are receipts: the bytes
    already shipped to an external service (CompanyCam, Drive). Losing
    a receipt at most costs one wasted retry roundtrip because the
    external service dedupes server-side. Losing a non-receipt row
    deletes raw photo bytes the agent still has not acted on. So we
    evict receipts first; non-receipt bytes only roll off once every
    receipt is gone. Within each partition we keep the
    ``expires_at asc`` order from the underlying query, which is what
    "oldest first" means at the SQL level.
    """
    async with db_session_async() as db:
        rows = list(
            (
                await db.execute(
                    select(StagedMedia)
                    .where(StagedMedia.user_id == user_id)
                    .order_by(StagedMedia.expires_at.asc())
                )
            )
            .scalars()
            .all()
        )
        if len(rows) <= STAGING_MAX_PER_USER:
            return
        receipts = [r for r in rows if r.upload_status is not None]
        non_receipts = [r for r in rows if r.upload_status is None]
        ordered = receipts + non_receipts
        overflow = ordered[: len(ordered) - STAGING_MAX_PER_USER]
        for row in overflow:
            _unlink_quiet(_resolve_disk_path(row.disk_path))
            logger.warning(
                "media_staging cap reached for user %s, evicted %s (handle=%s, was_receipt=%s)",
                user_id,
                row.original_url,
                row.handle,
                row.upload_status is not None,
            )
            await db.delete(row)
        await db.commit()


async def get_all_for_user(user_id: str) -> dict[str, bytes]:
    """Return non-expired staged bytes for a user as ``{original_url: bytes}``.

    Rows whose backing file has gone missing (e.g. a partial write or a
    manual scrub) are pruned in passing so subsequent calls don't keep
    walking dead rows.
    """
    now = datetime.now(UTC)
    result: dict[str, bytes] = {}
    orphan_ids: list[str] = []

    db = AsyncSessionLocal()
    try:
        rows = (
            (
                await db.execute(
                    select(StagedMedia).where(
                        StagedMedia.user_id == user_id,
                        StagedMedia.expires_at > now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            content = _read_bytes(_resolve_disk_path(row.disk_path))
            if content is None:
                orphan_ids.append(row.id)
                continue
            result[row.original_url] = content
    finally:
        await db.close()

    if orphan_ids:
        await _delete_rows_by_id(orphan_ids)
    return result


def _read_bytes(path: Path) -> bytes | None:
    """Read a staging file. ``None`` signals "treat the row as missing"."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Failed to read staged media file %s: %s", path, exc)
        return None


async def _delete_rows_by_id(ids: list[str]) -> None:
    if not ids:
        return
    async with db_session_async() as db:
        await db.execute(delete(StagedMedia).where(StagedMedia.id.in_(ids)))
        await db.commit()


async def list_urls_for_user(user_id: str) -> list[str]:
    """Return non-expired ``original_url`` values for a user, newest first.

    Cheaper than :func:`get_all_for_user` when the caller only needs to
    know which URLs are staged (e.g. to pick a fallback target for an
    arg-less ``upload_to_storage`` call). The caller loads bytes on
    demand for the chosen URL via :func:`resolve_media_ref`, avoiding
    the per-turn disk-read storm that would come from eager-loading
    everything just in case.

    Ordering is ``expires_at desc`` so the most recently staged or
    touched item lands first, matching "the photo the user probably
    meant" intuition.
    """
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        rows = (
            (
                await db.execute(
                    select(StagedMedia.original_url)
                    .where(
                        StagedMedia.user_id == user_id,
                        StagedMedia.expires_at > now,
                    )
                    .order_by(StagedMedia.expires_at.desc())
                )
            )
            .scalars()
            .all()
        )
    finally:
        await db.close()
    return list(rows)


async def get_mime_type(user_id: str, original_url: str) -> str | None:
    """Return the staged mime type for ``original_url``, or None.

    The download step knows the authoritative mime type; the LLM is
    guessing. ``upload_to_storage`` uses this to override its argument
    when available.
    """
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(
                select(StagedMedia).where(
                    StagedMedia.user_id == user_id,
                    StagedMedia.original_url == original_url,
                    StagedMedia.expires_at > now,
                )
            )
        ).scalar_one_or_none()
    finally:
        await db.close()
    return row.mime_type if row is not None else None


async def get_handle_for(user_id: str, original_url: str) -> str | None:
    """Return the staged handle for ``(user_id, original_url)`` or ``None``."""
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(
                select(StagedMedia).where(
                    StagedMedia.user_id == user_id,
                    StagedMedia.original_url == original_url,
                    StagedMedia.expires_at > now,
                )
            )
        ).scalar_one_or_none()
    finally:
        await db.close()
    return row.handle if row is not None else None


async def get_by_handle(handle: str) -> tuple[str, str, bytes, str] | None:
    """Look up a staged entry by its handle token.

    Returns ``(user_id, original_url, content, mime_type)`` or ``None``
    if the handle is unknown, expired, or the backing file has been
    swept from disk.
    """
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(
                select(StagedMedia).where(
                    StagedMedia.handle == handle,
                    StagedMedia.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        content = _read_bytes(_resolve_disk_path(row.disk_path))
        user_id = row.user_id
        original_url = row.original_url
        mime = row.mime_type
        orphan_id = row.id if content is None else None
    finally:
        await db.close()

    if orphan_id is not None:
        await _delete_rows_by_id([orphan_id])
        return None
    assert content is not None
    return user_id, original_url, content, mime


async def touch(handle: str, user_id: str) -> bool:
    """Extend the TTL on a staged entry because a tool referenced it.

    Long agent sessions span multiple back-and-forth turns; touching on
    every tool reference prevents a stale-TTL eviction mid-conversation.
    Returns True if the handle was found, owned by ``user_id``, and its
    TTL was extended.

    The ``user_id`` scope is defensive: today every caller does an
    ownership check via :func:`get_by_handle` first, but a future
    caller that forgets shouldn't be able to extend another user's TTL
    just by guessing a handle.
    """
    new_expires_at = datetime.now(UTC) + timedelta(seconds=STAGING_TTL_SECONDS)
    async with db_session_async() as db:
        result = cast(
            "CursorResult[object]",
            await db.execute(
                update(StagedMedia)
                .where(StagedMedia.handle == handle, StagedMedia.user_id == user_id)
                .values(expires_at=new_expires_at)
            ),
        )
        await db.commit()
        return result.rowcount > 0


async def resolve_media_ref(user_id: str, ref: str) -> tuple[str, bytes, str] | None:
    """Resolve a media reference that may be a handle or an original URL.

    The LLM sees media handles (``media_XXXXXX``) in the prompt and may
    pass them to tools that expect original URLs. This function accepts
    either form and returns ``(original_url, content, mime_type)`` when
    the referenced media is still staged for the given user.

    Returns ``None`` when the reference cannot be resolved (unknown
    handle, wrong user, expired entry, or URL not in staging).
    """
    # Try handle resolution first (handles start with "media_" but so
    # could a theoretical URL, so always check the handle index
    # regardless).
    entry = await get_by_handle(ref)
    if entry is not None:
        stored_uid, original_url, content, mime = entry
        if stored_uid == user_id:
            return original_url, content, mime
        return None

    # Fall back to URL lookup in the user's staged entries.
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(
                select(StagedMedia).where(
                    StagedMedia.user_id == user_id,
                    StagedMedia.original_url == ref,
                    StagedMedia.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        content = _read_bytes(_resolve_disk_path(row.disk_path))
        mime = row.mime_type
        orphan_id = row.id if content is None else None
    finally:
        await db.close()

    if orphan_id is not None:
        await _delete_rows_by_id([orphan_id])
        return None
    assert content is not None
    return ref, content, mime


async def evict(user_id: str, original_url: str) -> None:
    """Remove a staged entry (call after successful upload or explicit deny).

    The upload-record cache is intentionally NOT cleared here; a
    same-turn retry on the same handle still needs to find the receipt.
    Records age out via their own TTL.
    """
    async with db_session_async() as db:
        row = (
            await db.execute(
                select(StagedMedia).where(
                    StagedMedia.user_id == user_id,
                    StagedMedia.original_url == original_url,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        _unlink_quiet(_resolve_disk_path(row.disk_path))
        await db.delete(row)
        await db.commit()


async def evict_by_handle(handle: str) -> bool:
    """Remove a staged entry by its handle. Returns True if something was removed."""
    async with db_session_async() as db:
        row = (
            await db.execute(select(StagedMedia).where(StagedMedia.handle == handle))
        ).scalar_one_or_none()
        if row is None:
            return False
        _unlink_quiet(_resolve_disk_path(row.disk_path))
        await db.delete(row)
        await db.commit()
        return True


async def clear_user(user_id: str) -> None:
    """Drop all staged media and upload records for a user (primarily for tests).

    Upload receipts live on the same rows, so a single DELETE clears both.
    """
    async with db_session_async() as db:
        rows = list(
            (await db.execute(select(StagedMedia).where(StagedMedia.user_id == user_id)))
            .scalars()
            .all()
        )
        for row in rows:
            _unlink_quiet(_resolve_disk_path(row.disk_path))
        await db.execute(delete(StagedMedia).where(StagedMedia.user_id == user_id))
        await db.commit()


async def purge_expired() -> int:
    """Drop every expired row and its on-disk bytes. Returns rows deleted.

    Called from app startup so a fresh process doesn't accumulate dead
    rows across deploys. Inline ``stage`` cleanup keeps the table tidy
    during steady-state operation, but a missed-eviction-on-crash row
    can linger past its TTL until something explicitly sweeps it.
    """
    now = datetime.now(UTC)
    async with db_session_async() as db:
        rows = list(
            (await db.execute(select(StagedMedia).where(StagedMedia.expires_at <= now)))
            .scalars()
            .all()
        )
        for row in rows:
            _unlink_quiet(_resolve_disk_path(row.disk_path))
        if rows:
            await db.execute(delete(StagedMedia).where(StagedMedia.expires_at <= now))
            await db.commit()
        return len(rows)


# ---------------------------------------------------------------------------
# DB-backed upload receipts
# ---------------------------------------------------------------------------
#
# A handle that already shipped to an external service needs an idempotent
# answer on retry so the tool does not write a second copy. The receipt
# lives on the same ``staged_media`` row as the bytes it describes, so
# both share the same TTL and survive worker restarts. The lookup key
# matches either form of reference (URL or handle), mirroring
# :func:`resolve_media_ref`: the LLM is given handles in the prompt but
# tools call ``mark_uploaded`` after resolving to the URL, so the two
# sides of the idempotency contract must agree across both shapes.


async def mark_uploaded(
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

    ``original_url`` may be either the canonical URL the channel layer
    staged under or the short handle the LLM sees; the lookup matches
    either form. No-op when no ``staged_media`` row matches (e.g. the
    tool was called with a URL that was never staged, like a direct
    ``downloaded_media`` entry from a channel that does not stage).

    The OR-keyed match can in principle hit more than one row when a
    user happens to have one row whose ``original_url`` equals the ref
    and a different row whose ``handle`` equals it. We scope the UPDATE
    to a single id (most recently staged) so a stray match never writes
    a misleading receipt onto an unrelated row.
    """
    if not original_url:
        return
    now = datetime.now(UTC)
    async with db_session_async() as db:
        target_row_id = (
            await db.execute(
                select(StagedMedia.id)
                .where(
                    StagedMedia.user_id == user_id,
                    (StagedMedia.original_url == original_url)
                    | (StagedMedia.handle == original_url),
                    StagedMedia.expires_at > now,
                )
                .order_by(StagedMedia.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if target_row_id is None:
            logger.debug(
                "mark_uploaded: no staged_media row for user=%s ref=%s; receipt not persisted",
                user_id,
                original_url,
            )
            return
        await db.execute(
            update(StagedMedia)
            .where(StagedMedia.id == target_row_id)
            .values(
                upload_service=service,
                upload_external_id=external_id,
                upload_url=url,
                upload_target=target,
                upload_status=status,
                uploaded_at=now,
            )
        )
        await db.commit()


async def get_uploaded(user_id: str, original_url: str) -> UploadRecord | None:
    """Return the recorded upload receipt for ``(user_id, ref)``, if any.

    ``ref`` may be the staged handle (``media_XYZ``) or the original URL.
    Returns ``None`` when no row matches, the row has no recorded
    upload, or the row has expired.

    The OR-clause can in principle match more than one row (row A's
    ``original_url`` matches the ref while a different row B's
    ``handle`` matches it). The chance is near-zero given handles are
    48 bits of url-safe randomness behind a ``media_`` prefix, but we
    pick the most recently staged row deterministically rather than
    raising, so a benign retry never surfaces as a crash.
    """
    if not original_url:
        return None
    now = datetime.now(UTC)
    db = AsyncSessionLocal()
    try:
        row = (
            await db.execute(
                select(StagedMedia)
                .where(
                    StagedMedia.user_id == user_id,
                    (StagedMedia.original_url == original_url)
                    | (StagedMedia.handle == original_url),
                    StagedMedia.expires_at > now,
                )
                .order_by(StagedMedia.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    finally:
        await db.close()
    if row is None or row.upload_service is None:
        return None
    return UploadRecord(
        service=row.upload_service,
        external_id=row.upload_external_id or "",
        url=row.upload_url or "",
        target=row.upload_target or "",
        status=row.upload_status or "",
    )
