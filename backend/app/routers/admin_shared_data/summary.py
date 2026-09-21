"""Aggregate view: volume, top consenting users, and the user picker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.models import (
    ChatSession,
    HeartbeatLog,
    Message,
    ReportedConversation,
    Subscription,
    User,
)
from backend.app.query_helpers import count_rows, fetch_all, iso_or_none
from backend.app.schemas.shared_data import (
    SharedDataSummaryResponse,
    SharedDataTopUserItem,
    SharedDataUserItem,
    SharedDataUserListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)

router = APIRouter()


@router.get("/summary", response_model=SharedDataSummaryResponse)
async def get_shared_data_summary(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_SUMMARY)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataSummaryResponse:
    """Aggregate counts for the Overview "Research pilot" panel.

    Computes consenting-user totals, weekly activity counts (conversations
    + heartbeats + errors), open-report count, and a small leaderboard
    of the most active consenting users this week. Cheap enough to run
    on every Overview load: a handful of indexed COUNT and GROUP BY
    queries scoped to consenting user ids.

    Only counts and a small leaderboard surface here. Message bodies,
    memory text, and per-event content stay behind the existing
    per-conversation endpoints, which already PII-redact and audit-log
    every read.

    The "this week" window is the rolling 7 days ending now (UTC).
    """
    now = datetime.now(tz=UTC)
    week_ago = now - timedelta(days=7)

    consenting_user_count = (
        await db.execute(select(sa_func.count(User.id)).where(User.data_sharing_consent.is_(True)))
    ).scalar_one() or 0

    # Counts every user currently consenting whose data_sharing_consent_at
    # toggled within the week. The OSS column ticks on every change
    # (opt-in OR opt-out), so this surfaces "consent state moved
    # recently" rather than "first-time opt-ins". A user who toggled
    # off and back on within the week still counts.
    consents_changed_this_week = await count_rows(
        db, User.id, User.data_sharing_consent.is_(True), User.data_sharing_consent_at >= week_ago
    )

    consenting_user_ids = (
        (await db.execute(select(User.id).where(User.data_sharing_consent.is_(True))))
        .scalars()
        .all()
    )

    # Conversations with activity in the last 7 days, scoped to
    # consenting users only. We pick last_message_at over created_at so
    # the count tracks "what's actually being talked about" rather than
    # "what session shells got created".
    conversations_this_week = 0
    if consenting_user_ids:
        conversations_this_week = await count_rows(
            db,
            ChatSession.id,
            ChatSession.user_id.in_(consenting_user_ids),
            ChatSession.last_message_at >= week_ago,
        )

    # Heartbeat events scoped to consenting users in the last 7 days.
    # No "errors" sub-count here: the OSS heartbeat scheduler writes
    # ``action_type`` of ``send | skip | cleanup`` (see
    # backend/app/agent/heartbeat.py) and never ``error``, so an
    # error-typed metric would always read zero. Surface real error
    # signal via the Reported queue (already in this response) or via
    # structured logs.
    heartbeats_this_week = 0
    if consenting_user_ids:
        heartbeats_this_week = await count_rows(
            db,
            HeartbeatLog.id,
            HeartbeatLog.user_id.in_(consenting_user_ids),
            HeartbeatLog.created_at >= week_ago,
        )

    # Open reports: dismissed_at is null for an open report. Not
    # restricted to consenting users because reports are admin triage
    # signal independent of consent (the report row itself does not
    # surface message bodies; those are still gated).
    open_reports_count = await count_rows(
        db, ReportedConversation.id, ReportedConversation.dismissed_at.is_(None)
    )

    # Top-5 consenting users by message count this week. Joins
    # Message -> ChatSession to pull user_id, then groups. Sub-query
    # against consenting_user_ids keeps the candidate set bounded.
    top_user_rows: list[tuple[str, int]] = []
    if consenting_user_ids:
        rows = (
            await db.execute(
                select(ChatSession.user_id, sa_func.count(Message.id))
                .join(Message, Message.session_id == ChatSession.id)
                .where(
                    ChatSession.user_id.in_(consenting_user_ids),
                    Message.timestamp >= week_ago,
                )
                .group_by(ChatSession.user_id)
                .order_by(sa_func.count(Message.id).desc())
                .limit(5)
            )
        ).all()
        top_user_rows = [(uid, int(count)) for uid, count in rows]

    sub_emails = (
        {
            sub.user_id: sub.email
            for sub in (
                await db.execute(
                    select(Subscription).where(
                        Subscription.user_id.in_([uid for uid, _ in top_user_rows])
                    )
                )
            )
            .scalars()
            .all()
        }
        if top_user_rows
        else {}
    )
    user_ids = (
        {
            uid: u_id
            for uid, u_id in (
                await db.execute(
                    select(User.id, User.user_id).where(
                        User.id.in_([uid for uid, _ in top_user_rows])
                    )
                )
            ).all()
        }
        if top_user_rows
        else {}
    )

    top_users = [
        SharedDataTopUserItem(
            id=uid,
            email=sub_emails.get(uid, ""),
            user_id=user_ids.get(uid, ""),
            messages_this_week=int(count),
        )
        for uid, count in top_user_rows
    ]

    # Capture every count in the audit row so a forensic query against
    # admin_audit_logs.detail can reconstruct what the panel actually
    # showed at the time of the read. Earlier versions captured only
    # two fields, which made it harder to trace pilot trends backwards.
    ctx.detail = {
        "consenting_user_count": int(consenting_user_count),
        "consents_changed_this_week": int(consents_changed_this_week),
        "conversations_this_week": int(conversations_this_week),
        "heartbeats_this_week": int(heartbeats_this_week),
        "open_reports_count": int(open_reports_count),
        "top_user_count": len(top_users),
    }

    return SharedDataSummaryResponse(
        consenting_user_count=int(consenting_user_count),
        consents_changed_this_week=int(consents_changed_this_week),
        conversations_this_week=int(conversations_this_week),
        heartbeats_this_week=int(heartbeats_this_week),
        open_reports_count=int(open_reports_count),
        top_users_this_week=top_users,
    )


@router.get("/users", response_model=SharedDataUserListResponse)
async def list_shared_data_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_USERS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataUserListResponse:
    """List users who have opted into data sharing.

    Filtered to ``data_sharing_consent=True`` server-side; non-
    consenting rows never reach the response. ``conversation_count`` and
    ``last_message_at`` are per-user aggregates, so the admin can identify
    recent conversations without fetching a transcript.
    """
    total = (
        await db.execute(select(sa_func.count(User.id)).where(User.data_sharing_consent.is_(True)))
    ).scalar_one() or 0
    consenting_users = await fetch_all(
        db,
        select(User)
        .where(User.data_sharing_consent.is_(True))
        .order_by(User.data_sharing_consent_at.desc().nullslast())
        .offset(offset)
        .limit(limit),
    )

    user_ids = [u.id for u in consenting_users]
    if user_ids:
        activity_rows = (
            await db.execute(
                select(
                    ChatSession.user_id,
                    sa_func.count(ChatSession.id),
                    sa_func.max(ChatSession.last_message_at),
                )
                .where(ChatSession.user_id.in_(user_ids))
                .group_by(ChatSession.user_id)
            )
        ).all()
        activity = {
            user_id: (int(conversation_count), last_message_at)
            for user_id, conversation_count, last_message_at in activity_rows
        }
        sub_rows = (
            (await db.execute(select(Subscription).where(Subscription.user_id.in_(user_ids))))
            .scalars()
            .all()
        )
        sub_emails = {sub.user_id: sub.email for sub in sub_rows}
    else:
        activity = {}
        sub_emails = {}

    items = [
        SharedDataUserItem(
            id=u.id,
            user_id=u.user_id,
            email=sub_emails.get(u.id, ""),
            consent_at=iso_or_none(u.data_sharing_consent_at),
            conversation_count=activity.get(u.id, (0, None))[0],
            last_message_at=(
                activity[u.id][1].isoformat()
                if u.id in activity and activity[u.id][1] is not None
                else None
            ),
        )
        for u in consenting_users
    ]
    return SharedDataUserListResponse(total=int(total), items=items)
