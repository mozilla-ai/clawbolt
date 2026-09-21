"""A consenting user's profile, heartbeat sends, and working memory."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.models import (
    HeartbeatLog,
    MemoryDocument,
)
from backend.app.query_helpers import fetch_all, iso_or_none
from backend.app.routers.admin_shared_data.consent import (
    _parse_date_range,
    _require_consenting_user,
)
from backend.app.schemas.shared_data import (
    SharedDataHeartbeatLogItem,
    SharedDataHeartbeatLogListResponse,
    SharedDataMemoryDocumentResponse,
    SharedDataProfileResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/users/{user_id}/profile", response_model=SharedDataProfileResponse)
async def get_shared_data_profile(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_PROFILE)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataProfileResponse:
    """Return the consenting user's soul / user / heartbeat profile text.

    These three fields used to live on ``GET /admin/users/{id}`` until
    #336 dropped them. They are user-authored content (soul = how the
    agent should behave for this user; user_text = synthesized profile;
    heartbeat_text = proactive directives), so they belong behind the
    consent gate. Strings are passed through ``redact_pii`` even though
    they're plaintext at rest, because users sometimes paste contact
    info into their soul / heartbeat directives.

    Heartbeat config (opt-in flag, frequency, max_daily) is metadata
    that the slim ``/admin/users/{id}`` route already returns; we
    duplicate it here so an admin reviewing one consenting user has
    everything in one response without cross-route hopping.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    return SharedDataProfileResponse(
        user_id=user.id,
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
        soul_text=redact_pii(user.soul_text or ""),
        user_text=redact_pii(user.user_text or ""),
        heartbeat_text=redact_pii(user.heartbeat_text or ""),
        heartbeat_opt_in=user.heartbeat_opt_in,
        heartbeat_frequency=user.heartbeat_frequency,
        heartbeat_max_daily=user.heartbeat_max_daily,
    )


@router.get("/users/{user_id}/heartbeat-logs", response_model=SharedDataHeartbeatLogListResponse)
async def list_shared_data_heartbeat_logs(
    user_id: str,
    limit: int = Query(100, ge=1, le=500),
    start_date: str | None = Query(
        None, description="ISO-8601 timestamp; lower bound on created_at."
    ),
    end_date: str | None = Query(
        None, description="ISO-8601 timestamp; upper bound on created_at."
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_HEARTBEAT_LOGS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataHeartbeatLogListResponse:
    """Return heartbeat scheduler runs with their content fields.

    The non-consent variant ``/admin/users/{id}/heartbeat-logs`` returns
    only metadata (id, action_type, channel, created_at) since #336.
    For consenting users the full content surfaces here: ``message_text``
    (what the agent sent on this tick), ``reasoning`` (why it sent /
    skipped because it often quotes the user back to themselves), and ``tasks``
    (the serialized task state the LLM was deciding from). All three
    columns are envelope-encrypted at rest; ORM reads decrypt
    transparently and we redact PII shapes before serialization.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    range_filter = _parse_date_range(start_date, end_date)
    base_stmt = select(HeartbeatLog).where(HeartbeatLog.user_id == user.id)
    count_stmt = select(sa_func.count(HeartbeatLog.id)).where(HeartbeatLog.user_id == user.id)
    if range_filter is not None:
        start_dt, end_dt = range_filter
        if start_dt is not None:
            base_stmt = base_stmt.where(HeartbeatLog.created_at >= start_dt)
            count_stmt = count_stmt.where(HeartbeatLog.created_at >= start_dt)
        if end_dt is not None:
            base_stmt = base_stmt.where(HeartbeatLog.created_at <= end_dt)
            count_stmt = count_stmt.where(HeartbeatLog.created_at <= end_dt)
    rows = await fetch_all(
        db, base_stmt.order_by(HeartbeatLog.created_at.desc().nullslast()).limit(limit)
    )
    items = [
        SharedDataHeartbeatLogItem(
            id=row.id,
            action_type=row.action_type or "",
            channel=row.channel or "",
            message_text=redact_pii(row.message_text or ""),
            reasoning=redact_pii(row.reasoning or ""),
            tasks=redact_pii(row.tasks or ""),
            created_at=iso_or_none(row.created_at),
        )
        for row in rows
    ]
    total = (await db.execute(count_stmt)).scalar_one() or 0
    return SharedDataHeartbeatLogListResponse(
        user_id=user.id,
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
        items=items,
        total=int(total),
    )


@router.get("/users/{user_id}/memory", response_model=SharedDataMemoryDocumentResponse)
async def get_shared_data_memory(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_MEMORY)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataMemoryDocumentResponse:
    """Return the consenting user's MemoryDocument (memory + history).

    ``memory_text`` is the agent's working memory file (persistent
    notes, reminders, current context). ``history_text`` is the
    accumulated output of session compactions: each time a long
    session compacts, the LLM extracts durable facts and appends them
    here. Reading ``history_text`` is the closest persisted surface to
    a per-event compaction stream; per-event timing lives in
    ``logger.info("compaction.summary user=...")`` lines only and is
    not yet queryable.

    Both columns are envelope-encrypted at rest. A user with no
    document yet (never compacted, never wrote memory) returns empty
    strings rather than 404. Consenting and "no memory yet" is a
    valid combined state.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    doc = (
        await db.execute(select(MemoryDocument).where(MemoryDocument.user_id == user.id))
    ).scalar_one_or_none()
    return SharedDataMemoryDocumentResponse(
        user_id=user.id,
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
        memory_text=redact_pii(doc.memory_text if doc else ""),
        history_text=redact_pii(doc.history_text if doc else ""),
        updated_at=(doc.updated_at.isoformat() if doc and doc.updated_at else None),
    )
