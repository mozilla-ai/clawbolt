"""Compaction events, with the before/after snapshot decoded for display."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.models import (
    CompactionEvent,
)
from backend.app.query_helpers import fetch_all, iso_or_none
from backend.app.routers.admin_shared_data.consent import (
    _parse_date_range,
    _require_consenting_user,
)
from backend.app.schemas.shared_data import (
    SharedDataCompactionEventItem,
    SharedDataCompactionEventListResponse,
    SharedDataCompactionSnapshot,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii

router = APIRouter()


def _decode_snapshot(raw: str | None) -> SharedDataCompactionSnapshot:
    """Return a :class:`SharedDataCompactionSnapshot` for one snapshot column.

    OSS ``backend.app.agent.compaction._serialize_snapshot`` writes
    plaintext when the file is under
    ``settings.compaction_event_snapshot_max_bytes_per_file`` and a JSON
    truncation record otherwise (``{"truncated": True, "size_bytes",
    "head", "tail", "sha256"}``). The ORM decrypts the column to
    plaintext, so we just need to inspect the resulting string and
    decide which shape it is.

    Plaintext that happens to look like the truncation envelope
    (someone pasted ``{"truncated": true, ...}`` into MEMORY.md) would
    misclassify, so we only treat the string as a truncation record
    when it parses as a JSON object that has BOTH ``truncated=True`` and
    a numeric ``size_bytes``. This is a tighter test than just looking
    for the ``truncated`` key, which keeps user content from accidentally
    rendering as an "official" truncation banner.

    The plaintext / head / tail fields carry the same MEMORY.md /
    HISTORY.md / USER.md / SOUL.md content that the sibling ``/memory``
    and ``/profile`` endpoints redact before returning. Redaction
    happens here too so phone numbers, emails, and other PII shapes
    that the agent extracted into a memory file do not surface
    verbatim through the per-event snapshots. ``size_bytes`` and
    ``sha256`` describe the original (un-redacted) plaintext and are
    not user content, so they pass through untouched.
    """
    if raw is None:
        return SharedDataCompactionSnapshot()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return SharedDataCompactionSnapshot(text=redact_pii(raw))
    if (
        isinstance(parsed, dict)
        and parsed.get("truncated") is True
        and isinstance(parsed.get("size_bytes"), int)
    ):
        head = parsed.get("head")
        tail = parsed.get("tail")
        return SharedDataCompactionSnapshot(
            truncated=True,
            size_bytes=parsed.get("size_bytes"),
            head=redact_pii(head) if isinstance(head, str) else head,
            tail=redact_pii(tail) if isinstance(tail, str) else tail,
            sha256=parsed.get("sha256"),
        )
    return SharedDataCompactionSnapshot(text=redact_pii(raw))


@router.get(
    "/users/{user_id}/compaction-events",
    response_model=SharedDataCompactionEventListResponse,
)
async def list_shared_data_compaction_events(
    user_id: str,
    limit: int = Query(200, ge=1, le=1000),
    start_date: str | None = Query(
        None, description="ISO-8601 timestamp; lower bound on triggered_at."
    ),
    end_date: str | None = Query(
        None, description="ISO-8601 timestamp; upper bound on triggered_at."
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_COMPACTION_EVENTS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataCompactionEventListResponse:
    """Return per-event compaction metadata for one consenting user.

    Backed by the OSS ``compaction_events`` table (migrations 023 and
    030). The metadata columns (counts, timings, outcome flags) carry
    no user content, so no redaction is applied. Migration 030 added
    eight envelope-encrypted before/after snapshots (memory, history,
    user, soul); these decrypt to plaintext on read and ride through
    the response so an admin can see exactly what a compaction event
    rewrote across the four memory files. ``status`` is one of
    ``'pending'`` (sync watermark advanced, async LLM call still
    running or crashed) or ``'completed'``; legacy rows default to
    ``'completed'`` via the migration's server-side default.

    Snapshots that exceed
    ``settings.compaction_event_snapshot_max_bytes_per_file`` are
    stored as a JSON truncation record. We surface those as a flagged
    payload (``truncated=True`` plus head, tail, size, sha256) instead
    of dumping the JSON verbatim so the UI can render
    "truncated, N KB" with the head and tail visible inline.

    Ordered ``triggered_at desc`` so the most recent compaction shows
    first; that is the question admins almost always ask ("did this
    user just compact, and what did it cost?"). ``limit`` caps the
    response so a long-running user does not OOM the wire.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    range_filter = _parse_date_range(start_date, end_date)
    base_stmt = select(CompactionEvent).where(CompactionEvent.user_id == user.id)
    count_stmt = select(sa_func.count(CompactionEvent.id)).where(CompactionEvent.user_id == user.id)
    if range_filter is not None:
        start_dt, end_dt = range_filter
        if start_dt is not None:
            base_stmt = base_stmt.where(CompactionEvent.triggered_at >= start_dt)
            count_stmt = count_stmt.where(CompactionEvent.triggered_at >= start_dt)
        if end_dt is not None:
            base_stmt = base_stmt.where(CompactionEvent.triggered_at <= end_dt)
            count_stmt = count_stmt.where(CompactionEvent.triggered_at <= end_dt)
    rows = await fetch_all(
        db, base_stmt.order_by(CompactionEvent.triggered_at.desc().nullslast()).limit(limit)
    )
    items = [
        SharedDataCompactionEventItem(
            id=row.id,
            triggered_at=iso_or_none(row.triggered_at),
            duration_ms=row.duration_ms,
            trimmed_count=row.trimmed_count,
            trimmed_chars=row.trimmed_chars,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            min_message_seq=row.min_message_seq,
            max_message_seq=row.max_message_seq,
            status=row.status,
            memory_updated=row.memory_updated,
            user_profile_updated=row.user_profile_updated,
            soul_updated=row.soul_updated,
            summary_len=row.summary_len,
            memory_text_before=_decode_snapshot(row.memory_text_before),
            memory_text_after=_decode_snapshot(row.memory_text_after),
            history_text_before=_decode_snapshot(row.history_text_before),
            history_text_after=_decode_snapshot(row.history_text_after),
            user_text_before=_decode_snapshot(row.user_text_before),
            user_text_after=_decode_snapshot(row.user_text_after),
            soul_text_before=_decode_snapshot(row.soul_text_before),
            soul_text_after=_decode_snapshot(row.soul_text_after),
            prompt=_decode_snapshot(row.prompt_text),
            raw_response=_decode_snapshot(row.raw_response_text),
            parsed_response=_decode_snapshot(row.parsed_response_json),
        )
        for row in rows
    ]
    total = (await db.execute(count_stmt)).scalar_one() or 0
    return SharedDataCompactionEventListResponse(
        user_id=user.id,
        consent_at=iso_or_none(user.data_sharing_consent_at),
        items=items,
        total=int(total),
    )
