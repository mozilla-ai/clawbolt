"""Admin queue for user-initiated reports (issue #325 item 5).

Consumes ``ReportedConversation`` rows that OSS writes when a user
texts ``/report`` through any channel (see clawbolt#1102). Three
endpoints:

- ``GET /admin/reported-conversations`` lists open + dismissed
  reports with summary metadata. Reasons are PII-redacted before
  serialization.
- ``GET /admin/reported-conversations/{report_id}/messages`` returns
  the surrounding conversation window so an admin can read the
  context the user was reporting on. Messages go through
  ``redact_pii`` and the message at ``anchor_seq`` is flagged for
  the UI to highlight.
- ``POST /admin/reported-conversations/{report_id}/dismiss`` closes
  out a report by stamping ``dismissed_at`` and
  ``reviewed_admin_user_id``.

This is a separate router from ``/admin/shared-data`` because the
gating model is different: reports are user-initiated and admins
should always see them, regardless of the user's data sharing
consent state. A user filing a ``/report`` is implicitly consenting
to share the conversation context for that report.

Every read AND the dismiss mutation are audit-logged via
``audit_admin``. The dismiss row also persists the closing admin
inline on the ``ReportedConversation`` itself, but the audit log is
the canonical "who did what when" trail.

Routes use ``Depends(get_async_db)`` and an ``AsyncSession``.
``audit_admin`` records each read and dismiss action in a separate
async session so an audit-write failure cannot roll back the route's
work.
"""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.admin_dep import get_current_admin
from backend.app.database import get_async_db
from backend.app.models import ChatSession, Message, ReportedConversation, Subscription, User
from backend.app.schemas import (
    DismissReportedConversationResponse,
    ReportedConversationItem,
    ReportedConversationListResponse,
    ReportedConversationMessage,
    ReportedConversationMessageListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/reported-conversations", tags=["admin"])


@router.get("", response_model=ReportedConversationListResponse)
async def list_reported_conversations(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: str | None = Query(
        None,
        pattern="^(open|dismissed)$",
        description="Filter by status: 'open' (not yet dismissed) or 'dismissed'.",
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_REPORTED_CONVERSATIONS)),
    db: AsyncSession = Depends(get_async_db),
) -> ReportedConversationListResponse:
    """List reports for triage.

    No status filter returns both open and dismissed; ``status=open``
    or ``status=dismissed`` narrows. Open reports always sort first.
    """
    base_stmt = select(ReportedConversation)
    count_stmt = select(sa_func.count(ReportedConversation.id))
    if status == "open":
        base_stmt = base_stmt.where(ReportedConversation.dismissed_at.is_(None))
        count_stmt = count_stmt.where(ReportedConversation.dismissed_at.is_(None))
    elif status == "dismissed":
        base_stmt = base_stmt.where(ReportedConversation.dismissed_at.is_not(None))
        count_stmt = count_stmt.where(ReportedConversation.dismissed_at.is_not(None))

    total = (await db.execute(count_stmt)).scalar_one() or 0
    open_count = (
        await db.execute(
            select(sa_func.count(ReportedConversation.id)).where(
                ReportedConversation.dismissed_at.is_(None)
            )
        )
    ).scalar_one() or 0

    rows = (
        (
            await db.execute(
                base_stmt.order_by(
                    ReportedConversation.dismissed_at.is_not(None).asc(),
                    ReportedConversation.created_at.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Hydrate user emails (from Subscription) and session metadata in
    # batch instead of per-row. Two queries instead of 2N.
    user_ids = {r.user_id for r in rows}
    session_ids = {r.session_id for r in rows}
    admin_ids = {r.reviewed_admin_user_id for r in rows if r.reviewed_admin_user_id}

    sub_emails: dict[str, str] = {}
    if user_ids | admin_ids:
        for sub in (
            await db.execute(
                select(Subscription).where(Subscription.user_id.in_(user_ids | admin_ids))
            )
        ).scalars():
            sub_emails[sub.user_id] = sub.email

    session_meta: dict[int, ChatSession] = {}
    if session_ids:
        for cs in (
            await db.execute(select(ChatSession).where(ChatSession.id.in_(session_ids)))
        ).scalars():
            session_meta[cs.id] = cs

    items = [
        ReportedConversationItem(
            id=r.id,
            user_id=r.user_id,
            user_email=sub_emails.get(r.user_id, ""),
            session_id=session_meta[r.session_id].session_id
            if r.session_id in session_meta
            else "",
            channel=session_meta[r.session_id].channel if r.session_id in session_meta else "",
            anchor_seq=r.anchor_seq,
            reason=redact_pii(r.reason or ""),
            status="dismissed" if r.dismissed_at else "open",
            created_at=r.created_at.isoformat() if r.created_at else "",
            dismissed_at=r.dismissed_at.isoformat() if r.dismissed_at else None,
            reviewed_admin_email=(
                sub_emails.get(r.reviewed_admin_user_id, "") if r.reviewed_admin_user_id else None
            ),
        )
        for r in rows
    ]
    return ReportedConversationListResponse(
        total=int(total), open_count=int(open_count), items=items
    )


@router.get(
    "/{report_id}/messages",
    response_model=ReportedConversationMessageListResponse,
)
async def get_reported_conversation_messages(
    report_id: int,
    window: int = Query(
        20,
        ge=1,
        le=200,
        description="How many messages on either side of anchor_seq to include.",
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_REPORTED_CONVERSATION_MESSAGES)),
    db: AsyncSession = Depends(get_async_db),
) -> ReportedConversationMessageListResponse:
    """Return the messages around the report's anchor.

    Includes ``window`` messages on either side of ``anchor_seq``. If
    ``anchor_seq`` is NULL (the OSS handler couldn't capture one when
    the user reported on an empty session), returns the most recent
    ``window`` messages instead.

    All bodies pass through PII redaction before serialization.
    """
    report = (
        await db.execute(select(ReportedConversation).where(ReportedConversation.id == report_id))
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    ctx.target_user_id = report.user_id
    ctx.resource_type = "reported_conversation"
    ctx.resource_id = str(report_id)

    cs = (
        await db.execute(select(ChatSession).where(ChatSession.id == report.session_id))
    ).scalar_one_or_none()
    if cs is None:
        # The session was deleted (e.g. user account purge cascaded).
        # Surface an empty list rather than 500: the report still
        # exists for forensic purposes.
        return ReportedConversationMessageListResponse(
            report_id=report.id,
            session_id="",
            user_id=report.user_id,
            anchor_seq=report.anchor_seq,
            items=[],
        )

    messages_stmt = select(Message).where(Message.session_id == cs.id)
    if report.anchor_seq is not None:
        lo = max(1, report.anchor_seq - window)
        hi = report.anchor_seq + window
        messages_stmt = messages_stmt.where(Message.seq >= lo, Message.seq <= hi)
    messages = (
        (await db.execute(messages_stmt.order_by(Message.seq.asc()).limit(2 * window + 1)))
        .scalars()
        .all()
    )

    items = [
        ReportedConversationMessage(
            seq=m.seq,
            direction=m.direction,
            body=redact_pii(m.body or ""),
            timestamp=m.timestamp.isoformat() if m.timestamp else None,
            is_anchor=(report.anchor_seq is not None and m.seq == report.anchor_seq),
        )
        for m in messages
    ]
    return ReportedConversationMessageListResponse(
        report_id=report.id,
        session_id=cs.session_id,
        user_id=report.user_id,
        anchor_seq=report.anchor_seq,
        items=items,
    )


@router.post(
    "/{report_id}/dismiss",
    response_model=DismissReportedConversationResponse,
)
async def dismiss_reported_conversation(
    report_id: int,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DISMISS_REPORTED_CONVERSATION)),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_async_db),
) -> DismissReportedConversationResponse:
    """Mark a report as dismissed.

    Stamps ``dismissed_at`` (NOW) and ``reviewed_admin_user_id`` (the
    calling admin's id). Returns 404 if the report doesn't exist, 400
    if it's already dismissed (idempotent re-dismiss is a no-op but we
    surface it so the admin UI can show a clear error rather than
    silently swallowing the click).
    """
    report = (
        await db.execute(select(ReportedConversation).where(ReportedConversation.id == report_id))
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.dismissed_at is not None:
        raise HTTPException(status_code=400, detail="Report already dismissed")

    ctx.target_user_id = report.user_id
    ctx.resource_type = "reported_conversation"
    ctx.resource_id = str(report_id)

    now = datetime.datetime.now(datetime.UTC)
    report.dismissed_at = now
    report.reviewed_admin_user_id = admin.id
    await db.commit()

    return DismissReportedConversationResponse(
        id=report.id,
        dismissed_at=now.isoformat(),
        reviewed_admin_user_id=admin.id,
    )
