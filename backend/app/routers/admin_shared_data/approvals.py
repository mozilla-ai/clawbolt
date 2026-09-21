"""The tool-approval lifecycle trail for a consenting user."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.approval import get_approval_event_store
from backend.app.database import get_async_db
from backend.app.query_helpers import iso_or_none
from backend.app.routers.admin_shared_data.consent import (
    _parse_date_range,
    _require_consenting_user,
)
from backend.app.schemas.shared_data import (
    SharedDataApprovalEventItem,
    SharedDataApprovalEventListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii

router = APIRouter()


@router.get(
    "/users/{user_id}/approval-events",
    response_model=SharedDataApprovalEventListResponse,
)
async def list_shared_data_approval_events(
    user_id: str,
    limit: int = Query(500, ge=1, le=2000),
    start_date: str | None = Query(
        None, description="ISO-8601 timestamp; lower bound on created_at."
    ),
    end_date: str | None = Query(
        None, description="ISO-8601 timestamp; upper bound on created_at."
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_APPROVAL_EVENTS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataApprovalEventListResponse:
    """Return per-event tool-approval lifecycle for one consenting user.

    Backed by the OSS ``approval_events`` table (migration 028). The
    agent's approval gate writes one row per transition: ``requested``
    when a tool with ASK policy fires, ``decided`` (with the
    ApprovalDecision) when the user replies, ``timed_out`` if the user
    never answers, and ``recovered`` if a worker crash left an orphan
    that the next boot cleaned up. Surfacing this stream lets admins
    see when the agent was blocked on a permission prompt and how the
    request resolved, which the conversation transcript alone cannot
    show (prompts ride through the messages table indistinguishably
    from ordinary replies).

    ``description`` is the human-readable text shown to the user in
    the prompt body. It can echo user-pasted content (filenames, URLs,
    quoted message text), so it is PII-redacted before serialization.
    ``channel`` and ``chat_id`` are infrastructure metadata that route
    the prompt; they are not redacted.

    Ordered ``created_at asc`` so a request/decided pair stays adjacent
    in the response, matching how the activity feed will render them.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    range_filter = _parse_date_range(start_date, end_date)
    since: datetime | None = None
    upper: datetime | None = None
    if range_filter is not None:
        since, upper = range_filter
    records = await get_approval_event_store().list_for_user(user.id, limit=limit, since=since)
    if upper is not None:
        records = [r for r in records if r.created_at <= upper]
    items = [
        SharedDataApprovalEventItem(
            id=r.id,
            event_type=r.event_type,
            tool_name=r.tool_name,
            description=redact_pii(r.description),
            channel=r.channel,
            chat_id=r.chat_id,
            decision=r.decision,
            created_at=iso_or_none(r.created_at),
        )
        for r in records
    ]
    return SharedDataApprovalEventListResponse(
        user_id=user.id,
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
        items=items,
        total=len(items),
    )
