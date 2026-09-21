"""Admin console: heartbeat logs, LLM usage, media staging, and webhook forensics."""

from __future__ import annotations

import datetime
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.media_staging import STAGING_MAX_PER_USER
from backend.app.agent.user_db import get_user_store
from backend.app.database import get_async_db
from backend.app.models import (
    ChatSession,
    HeartbeatLog,
    IdempotencyKey,
    LLMPayloadCapture,
    LLMUsageLog,
    Message,
    StagedMedia,
    User,
)
from backend.app.query_helpers import count_rows, fetch_all, iso, iso_or_none
from backend.app.schemas.admin import (
    StagedMediaItem,
    StagedMediaListResponse,
    WebhookEventItem,
    WebhookEventListResponse,
)
from backend.app.schemas.heartbeat import AdminHeartbeatLogItem, AdminHeartbeatLogListResponse
from backend.app.schemas.llm import (
    LLMUsageLogItem,
    LLMUsageLogListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.llm_payload_capture import purge_user_captures

router = APIRouter()


@router.get("/users/{user_id}/heartbeat-logs", response_model=AdminHeartbeatLogListResponse)
async def get_user_heartbeat_logs(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_HEARTBEAT_LOGS)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminHeartbeatLogListResponse:
    """List heartbeat log metadata for a specific user, most recent first.

    Slimmed in #325 work item 2: ``message_text``, ``reasoning``, and
    ``tasks`` were stripped from this response. Heartbeat content
    surfaces only via the consent-gated paths once items 3 + 4 land.
    """
    ctx.resource_type = "heartbeat_logs"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    total = await count_rows(db, HeartbeatLog.id, HeartbeatLog.user_id == user_id)

    logs = await fetch_all(
        db,
        select(HeartbeatLog)
        .where(HeartbeatLog.user_id == user_id)
        .order_by(HeartbeatLog.created_at.desc())
        .limit(limit),
    )

    return AdminHeartbeatLogListResponse(
        total=total,
        items=[
            AdminHeartbeatLogItem(
                id=log.id,
                user_id=log.user_id,
                action_type=getattr(log, "action_type", None) or "send",
                channel=getattr(log, "channel", None) or "",
                created_at=iso(log.created_at),
            )
            for log in logs
        ],
    )


@router.get("/users/{user_id}/llm-usage-logs", response_model=LLMUsageLogListResponse)
async def get_user_llm_usage_logs(
    user_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_USAGE_LOGS)),
    db: AsyncSession = Depends(get_async_db),
) -> LLMUsageLogListResponse:
    """List per-call LLM usage logs for a specific user, most recent first.

    Each row records a single LLM call: provider, model, purpose
    (primary / vision / heartbeat / compaction / etc.), token counts,
    and cost in USD. Purpose lets you spot a runaway compaction loop
    or a heartbeat model burning cache misses.

    The audit dependency writes one row when the route exits, matching
    the policy on the user-detail endpoint.
    """
    ctx.resource_type = "llm_usage_logs"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    total = await count_rows(db, LLMUsageLog.id, LLMUsageLog.user_id == user_id)

    logs = await fetch_all(
        db,
        select(LLMUsageLog)
        .where(LLMUsageLog.user_id == user_id)
        .order_by(LLMUsageLog.created_at.desc())
        .limit(limit),
    )

    return LLMUsageLogListResponse(
        total=total,
        items=[
            LLMUsageLogItem(
                id=log.id,
                timestamp=iso(log.created_at),
                endpoint=log.endpoint,
                provider=log.provider,
                model=log.model,
                purpose=log.purpose,
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                total_tokens=log.total_tokens,
                cost_usd=str(log.cost),
                pricing_available=log.pricing_available,
                cache_creation_input_tokens=log.cache_creation_input_tokens,
                cache_read_input_tokens=log.cache_read_input_tokens,
            )
            for log in logs
        ],
    )


@router.get("/users/{user_id}/staged-media", response_model=StagedMediaListResponse)
async def get_user_staged_media(
    user_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_STAGED_MEDIA)),
    db: AsyncSession = Depends(get_async_db),
) -> StagedMediaListResponse:
    """List staged-media rows for a user, newest first, with cap context.

    Returns ``total`` / ``active`` / ``uploaded`` / ``cap`` counts so a
    glance answers "is this user pinned at the cap, and are all 50
    slots upload receipts?" -- the shape that proves the receipts-pin
    -the-cap symptom without scrolling the row list.
    """
    ctx.resource_type = "staged_media"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    now = datetime.datetime.now(datetime.UTC)
    total = await count_rows(db, StagedMedia.id, StagedMedia.user_id == user_id)
    active = await count_rows(
        db, StagedMedia.id, StagedMedia.user_id == user_id, StagedMedia.expires_at > now
    )
    uploaded = await count_rows(
        db, StagedMedia.id, StagedMedia.user_id == user_id, StagedMedia.upload_status.isnot(None)
    )

    rows = await fetch_all(
        db,
        select(StagedMedia)
        .where(StagedMedia.user_id == user_id)
        .order_by(StagedMedia.created_at.desc())
        .limit(limit),
    )

    return StagedMediaListResponse(
        total=total,
        active=active,
        uploaded=uploaded,
        cap=STAGING_MAX_PER_USER,
        items=[
            StagedMediaItem(
                handle=row.handle,
                original_url=row.original_url,
                mime_type=row.mime_type,
                created_at=iso(row.created_at),
                expires_at=iso(row.expires_at),
                upload_service=row.upload_service,
                upload_status=row.upload_status,
                uploaded_at=iso_or_none(row.uploaded_at),
            )
            for row in rows
        ],
    )


@router.get("/users/{user_id}/webhook-events", response_model=WebhookEventListResponse)
async def get_user_webhook_events(
    user_id: str,
    since: str = Query(
        "",
        description=(
            "ISO timestamp lower bound. Default returns the most recent rows. "
            "Format ``2026-05-21T00:00:00+00:00``."
        ),
    ),
    channel_prefix: str = Query(
        "bb_",
        description=(
            "Restrict to ``idempotency_keys`` whose ``external_id`` starts "
            "with this prefix. Defaults to ``bb_`` (BlueBubbles)."
        ),
    ),
    include_orphans: bool = Query(
        False,
        description=(
            "Also return idempotency rows in the window that did NOT produce "
            "a Message row. Orphans are not user-scoped (the idempotency "
            "table has no user_id column) so they may belong to another "
            "tenant; only opt in for whole-server investigations."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_WEBHOOK_EVENTS)),
    db: AsyncSession = Depends(get_async_db),
) -> WebhookEventListResponse:
    """List webhook dedup events for this user, newest first.

    Joins ``idempotency_keys`` to ``messages`` via the external_id so
    each row carries whether a Message persisted. Default behavior is
    to return only events tied to this user; setting
    ``include_orphans=true`` adds rows where no Message landed (those
    reveal approval-gate consumption or consumer-side failures, but
    cannot be attributed to a specific user so they are off by
    default to avoid cross-tenant noise).
    """
    ctx.resource_type = "webhook_events"
    ctx.resource_id = user_id
    ctx.detail = {
        "limit": limit,
        "channel_prefix": channel_prefix,
        "since": since,
        "include_orphans": include_orphans,
    }

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    since_dt: datetime.datetime | None = None
    if since:
        try:
            since_dt = datetime.datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid since timestamp: {exc}",
            ) from exc

    # LEFT JOIN keeps a row even when no Message landed. Two flavors:
    # - Default: only rows whose joined Message belongs to ``user_id``
    #   (an INNER-join semantic implemented as a WHERE clause that
    #   discards orphans).
    # - ``include_orphans=true``: also keep rows with NULL message join,
    #   which surfaces cross-user orphans for diagnostic use.
    stmt = (
        select(
            IdempotencyKey,
            Message,
            ChatSession.user_id.label("msg_user_id"),
        )
        .join(Message, Message.external_message_id == IdempotencyKey.external_id, isouter=True)
        .join(ChatSession, ChatSession.id == Message.session_id, isouter=True)
        .where(IdempotencyKey.external_id.startswith(channel_prefix))
    )
    if since_dt is not None:
        stmt = stmt.where(IdempotencyKey.created_at >= since_dt)
    if include_orphans:
        stmt = stmt.where((ChatSession.user_id == user_id) | (Message.id.is_(None)))
    else:
        stmt = stmt.where(ChatSession.user_id == user_id)
    stmt = stmt.order_by(IdempotencyKey.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).all()

    items: list[WebhookEventItem] = []
    for key_row, msg_row, msg_user_id in rows:
        media_count = 0
        if msg_row is not None and msg_row.media_urls_json:
            try:
                parsed = json.loads(msg_row.media_urls_json)
                media_count = len(parsed) if isinstance(parsed, list) else 0
            except (ValueError, TypeError):
                media_count = 0
        items.append(
            WebhookEventItem(
                external_id=key_row.external_id,
                created_at=iso(key_row.created_at),
                message_persisted=msg_row is not None,
                user_id=msg_user_id,
                message_timestamp=(
                    msg_row.timestamp.isoformat() if msg_row and msg_row.timestamp else None
                ),
                media_count=media_count,
            )
        )

    return WebhookEventListResponse(total=len(items), items=items)


@router.get("/users/{user_id}/llm-payloads")
async def export_user_llm_payloads(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.EXPORT_LLM_PAYLOADS)),
    db: AsyncSession = Depends(get_async_db),
) -> JSONResponse:
    """Download the captured LLM request payloads for one user.

    Returns the previous-era and current-era payload snapshots stored
    by ``llm_payload_capture`` for users who have toggled
    ``data_sharing_consent``. Non-consenting users are not captured in
    the first place, so 404 is returned both when no row exists and
    when the user has revoked consent (which deletes the row).

    Response is served with ``Content-Disposition: attachment`` so the
    admin frontend can drop it to disk for offline analysis without
    rendering the JSON in-page.
    """
    ctx.resource_type = "llm_payload_captures"
    ctx.resource_id = user_id

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    # Defense-in-depth: if consent has been revoked since the last
    # capture, purge any lingering row and 404. The capture observer
    # also lazy-cleans on the next LLM call, but an admin reading
    # before that fires would otherwise see stale data.
    #
    # The ``db.commit()`` here is safe alongside the audit dependency:
    # ``audit_admin`` writes its row in a fresh session bound to the
    # engine (see services/admin_audit.py), not in ``db``. If that
    # contract ever changes, the commit-then-raise pattern would skip
    # the audit row for revoked-consent reads -- worth a regression
    # test if the audit dep is refactored to share ``db``.
    if not user.data_sharing_consent:
        await purge_user_captures(db, user_id)
        await db.commit()
        raise HTTPException(status_code=404, detail="No captured payloads for this user")

    row = (
        await db.execute(select(LLMPayloadCapture).where(LLMPayloadCapture.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No captured payloads for this user")

    body = {
        "user_id": user_id,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        # ``latest_capture_at`` mirrors ``current_era.captured_at`` at the
        # top level so admins can eyeball "did this user just hit a runaway
        # context loop?" without having to dig into the nested object.
        "latest_capture_at": row.current_era_captured_at.isoformat(),
        "current_era": {
            "payload": row.current_era_payload,
            "captured_at": row.current_era_captured_at.isoformat(),
            "min_message_seq": row.current_era_min_message_seq,
            "request_id": row.current_era_request_id,
            "payload_bytes": row.current_era_payload_bytes,
            "response": row.current_era_response,
            "response_captured_at": (
                row.current_era_response_captured_at.isoformat()
                if row.current_era_response_captured_at is not None
                else None
            ),
            "response_bytes": row.current_era_response_bytes,
        },
        "previous_era": (
            {
                "payload": row.previous_era_payload,
                "captured_at": (
                    row.previous_era_captured_at.isoformat()
                    if row.previous_era_captured_at is not None
                    else None
                ),
                "min_message_seq": row.previous_era_min_message_seq,
                "request_id": row.previous_era_request_id,
                "payload_bytes": row.previous_era_payload_bytes,
                "response": row.previous_era_response,
                "response_captured_at": (
                    row.previous_era_response_captured_at.isoformat()
                    if row.previous_era_response_captured_at is not None
                    else None
                ),
                "response_bytes": row.previous_era_response_bytes,
            }
            if row.previous_era_payload is not None
            else None
        ),
    }
    ctx.detail = {
        "current_bytes": row.current_era_payload_bytes,
        "previous_bytes": row.previous_era_payload_bytes,
        "has_previous": row.previous_era_payload is not None,
        "current_response_bytes": row.current_era_response_bytes,
        "previous_response_bytes": row.previous_era_response_bytes,
        "has_current_response": row.current_era_response is not None,
    }
    return JSONResponse(
        content=body,
        headers={"Content-Disposition": (f'attachment; filename="llm-payloads-{user_id}.json"')},
    )
