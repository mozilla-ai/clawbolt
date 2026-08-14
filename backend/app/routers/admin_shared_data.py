"""Consent-gated admin endpoints for user content (issue #325 item 3).

Surfaces message bodies / conversation transcripts to admins for ONLY
those users who set ``User.data_sharing_consent=True`` (the OSS column
added in clawbolt#1100). Content is PII-redacted before serialization.

Endpoints:

- ``GET /admin/shared-data/users`` lists every consenting user with a
  conversation count, so an admin can pick someone whose data they
  want to review.
- ``GET /admin/shared-data/users/{user_id}/conversations`` lists
  conversations for one consenting user.
- ``GET /admin/shared-data/conversations/{session_id}/turns`` returns
  the conversation grouped into agent turns: each turn pairs the
  user's inbound message with the agent's outbound reply(ies) and the
  tool calls fired in between, so admins can debug "why did the agent
  do that on this turn?" without reading raw JSON. PII-redacted.
- ``GET /admin/shared-data/users/{user_id}/profile`` returns the
  agent personality + synthesized profile + heartbeat directives.
- ``GET /admin/shared-data/users/{user_id}/heartbeat-logs`` returns
  heartbeat scheduler runs with their content columns (the slim
  /admin/users/{id}/heartbeat-logs returns metadata only).
- ``GET /admin/shared-data/users/{user_id}/memory`` returns the
  MemoryDocument (working memory + accumulated compaction history).
- ``GET /admin/shared-data/users/{user_id}/compaction-events``
  returns per-event compaction metadata (when, sizes, what got
  updated) backed by the OSS ``compaction_events`` table.
- ``GET /admin/shared-data/users/{user_id}/export`` bundles every
  consent-gated surface for one user into a single response so a CLI
  caller can answer "what's wrong with this user's experience?"
  without walking the per-surface endpoints.

Every read writes one ``AdminAuditLog`` row via ``audit_admin``,
recording (admin, target_user, action, resource_id) so a forensic
query can answer "which admin read which consenting user's content,
and when?" The OSS ``/admin/shared-data`` reference (in octonous) is
NOT audit-logged; we close that gap here because this deployment
already has the audit infrastructure in place from PR #330.

Non-consenting users return 403 even when the calling admin has
permission; consent is the gate, not admin role. A user who toggles
consent off mid-investigation will start returning 403 on the next
admin read, which is the correct behavior — admins lose access the
moment the user revokes.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.approval import get_approval_event_store
from backend.app.agent.context import StoredToolInteraction
from backend.app.database import get_async_db
from backend.app.models import (
    ChatSession,
    CompactionEvent,
    HeartbeatLog,
    LLMUsageLog,
    MemoryDocument,
    Message,
    ReportedConversation,
    Subscription,
    User,
)
from backend.app.schemas import (
    SharedDataApprovalEventItem,
    SharedDataApprovalEventListResponse,
    SharedDataCompactionEventItem,
    SharedDataCompactionEventListResponse,
    SharedDataCompactionSnapshot,
    SharedDataConversationItem,
    SharedDataConversationTurnsResponse,
    SharedDataExportResponse,
    SharedDataExportSummary,
    SharedDataExportTopTool,
    SharedDataHeartbeatLogItem,
    SharedDataHeartbeatLogListResponse,
    SharedDataMemoryDocumentResponse,
    SharedDataMessageItem,
    SharedDataProfileResponse,
    SharedDataReceipt,
    SharedDataSummaryResponse,
    SharedDataToolCall,
    SharedDataTopUserItem,
    SharedDataTurn,
    SharedDataUserItem,
    SharedDataUserListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii, redact_pii_recursive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/shared-data", tags=["admin"])


def _parse_date_range(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None] | None:
    """Parse optional ISO-8601 date / datetime strings to a ``(start, end)`` tuple.

    Returns ``None`` when both inputs are absent or empty (no filter
    applies). Raises ``HTTPException(400)`` on malformed inputs so the
    admin gets a clear failure rather than silent permissive behavior.

    Accepts both date-only (``2026-05-01``) and full ISO-8601
    timestamps. A bare date is interpreted as the start (00:00:00 UTC)
    or end (23:59:59 UTC) of that day depending on which slot it
    occupies, so the human-friendly ``start=2026-05-01 end=2026-05-31``
    behaves intuitively as a closed-closed range.
    """
    from datetime import UTC, time

    if not start_date and not end_date:
        return None

    def _parse(value: str, *, end_of_day: bool) -> datetime:
        try:
            # ``fromisoformat`` accepts both ``YYYY-MM-DD`` and full
            # ISO-8601 timestamps in modern Python; try the date-only
            # path first so we can lift to start/end-of-day.
            if len(value) == 10 and value.count("-") == 2:
                d = datetime.fromisoformat(value).date()
                return datetime.combine(d, time(23, 59, 59) if end_of_day else time.min, tzinfo=UTC)
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date '{value}': {exc}",
            ) from exc

    start_dt = _parse(start_date, end_of_day=False) if start_date else None
    end_dt = _parse(end_date, end_of_day=True) if end_date else None
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        raise HTTPException(
            status_code=400,
            detail=f"start_date {start_date} is after end_date {end_date}",
        )
    return (start_dt, end_dt)


async def _require_consenting_user(db: AsyncSession, user_id: str) -> User:
    """Resolve *user_id* to a User row that has data sharing consent.

    Returns the User on success; raises ``HTTPException(403)`` when the
    user exists but has not consented, or ``HTTPException(404)`` when
    the user doesn't exist. The 403/404 split matters for forensic
    purposes: a 403 leaves an audit row showing "admin tried to read
    a non-consenting user's data" which is itself useful signal.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.data_sharing_consent:
        raise HTTPException(
            status_code=403,
            detail="User has not consented to data sharing.",
        )
    return user


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
    consents_changed_this_week = (
        await db.execute(
            select(sa_func.count(User.id)).where(
                User.data_sharing_consent.is_(True),
                User.data_sharing_consent_at >= week_ago,
            )
        )
    ).scalar_one() or 0

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
        conversations_this_week = (
            await db.execute(
                select(sa_func.count(ChatSession.id)).where(
                    ChatSession.user_id.in_(consenting_user_ids),
                    ChatSession.last_message_at >= week_ago,
                )
            )
        ).scalar_one() or 0

    # Heartbeat events scoped to consenting users in the last 7 days.
    # No "errors" sub-count here: the OSS heartbeat scheduler writes
    # ``action_type`` of ``send | skip | cleanup`` (see
    # backend/app/agent/heartbeat.py) and never ``error``, so an
    # error-typed metric would always read zero. Surface real error
    # signal via the Reported queue (already in this response) or via
    # structured logs.
    heartbeats_this_week = 0
    if consenting_user_ids:
        heartbeats_this_week = (
            await db.execute(
                select(sa_func.count(HeartbeatLog.id)).where(
                    HeartbeatLog.user_id.in_(consenting_user_ids),
                    HeartbeatLog.created_at >= week_ago,
                )
            )
        ).scalar_one() or 0

    # Open reports: dismissed_at is null for an open report. Not
    # restricted to consenting users because reports are admin triage
    # signal independent of consent (the report row itself does not
    # surface message bodies — those are still gated).
    open_reports_count = (
        await db.execute(
            select(sa_func.count(ReportedConversation.id)).where(
                ReportedConversation.dismissed_at.is_(None)
            )
        )
    ).scalar_one() or 0

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
    consenting_users = (
        (
            await db.execute(
                select(User)
                .where(User.data_sharing_consent.is_(True))
                .order_by(User.data_sharing_consent_at.desc().nullslast())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
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
            consent_at=(
                u.data_sharing_consent_at.isoformat() if u.data_sharing_consent_at else None
            ),
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


@router.get(
    "/users/{user_id}/conversation",
    response_model=SharedDataConversationItem,
)
async def get_shared_data_conversation(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_CONVERSATIONS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataConversationItem:
    """Return the consenting user's single conversation.

    Each user has at most one conversation (enforced by the
    ``uq_sessions_user_id`` constraint on OSS). 404s if the user has
    no conversation yet, which is normal for a freshly onboarded user
    who hasn't sent a first message.
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    session = (
        await db.execute(select(ChatSession).where(ChatSession.user_id == user.id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="No conversation yet")

    message_count = (
        await db.execute(select(sa_func.count(Message.id)).where(Message.session_id == session.id))
    ).scalar_one() or 0
    return SharedDataConversationItem(
        session_id=session.session_id,
        channel=session.channel or "",
        created_at=session.created_at.isoformat() if session.created_at else None,
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=int(message_count),
        last_trim_seq=session.last_trim_seq,
    )


def _parse_tool_interactions(raw: str) -> list[StoredToolInteraction]:
    """Parse a message's ``tool_interactions_json``, dropping invalid entries.

    Mirrors ``backend.app.agent.context._parse_tool_interactions`` so we
    do not reach into a private OSS helper. Items that fail validation
    are skipped silently rather than raising, so a single corrupt row
    does not blow up an admin's read of a long conversation.
    """
    if not raw or raw == "[]":
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[StoredToolInteraction] = []
    for entry in parsed:
        with contextlib.suppress(Exception):
            out.append(StoredToolInteraction.model_validate(entry))
    return out


def _redact_tool_call(interaction: StoredToolInteraction) -> SharedDataToolCall:
    """Convert one StoredToolInteraction into a redacted SharedDataToolCall.

    ``args`` walks recursively (so nested dict / list values get
    redacted at every string leaf). ``result`` is treated as a single
    string. The receipt's ``action`` / ``target`` / ``url`` are also
    string-redacted because receipts often surface third-party deep
    links and human-readable target names that may carry PII.
    """
    receipt: SharedDataReceipt | None = None
    if interaction.receipt is not None:
        receipt = SharedDataReceipt(
            action=redact_pii(interaction.receipt.action or ""),
            target=redact_pii(interaction.receipt.target or ""),
            url=redact_pii(interaction.receipt.url) if interaction.receipt.url else None,
        )
    return SharedDataToolCall(
        tool_call_id=interaction.tool_call_id,
        name=interaction.name,
        args=redact_pii_recursive(interaction.args),
        result=redact_pii(interaction.result or ""),
        is_error=interaction.is_error,
        receipt=receipt,
    )


def _group_turns(messages: list[Message]) -> list[SharedDataTurn]:
    """Group ordered messages into turns.

    A turn starts at an inbound (user) message and includes every
    outbound (agent) message that follows until the next inbound or
    end of conversation. Tool calls aggregate from every outbound
    message in the turn, in seq order, so a multi-message agent reply
    that fires tools across two outbound rows still surfaces as a
    single turn with the full tool list.

    Conversations that begin with an outbound message (e.g. the agent
    initiated the turn from a heartbeat tick) get a leading turn with
    no ``user_message``, just ``agent_reply`` + ``tool_calls``. This
    keeps every persisted message visible to the admin without
    inventing synthetic inbounds.
    """
    turns: list[SharedDataTurn] = []
    pending_user: Message | None = None
    pending_agent_msgs: list[Message] = []
    pending_tools: list[SharedDataToolCall] = []
    turn_index = 0

    def flush() -> None:
        nonlocal pending_user, pending_agent_msgs, pending_tools, turn_index
        if pending_user is None and not pending_agent_msgs:
            return
        agent_reply: SharedDataMessageItem | None = None
        if pending_agent_msgs:
            # Concatenate bodies of multi-message agent replies so admins see
            # the complete reply as one block rather than chasing seq numbers.
            last = pending_agent_msgs[-1]
            joined_body = "\n".join(m.body for m in pending_agent_msgs if m.body)
            # Same join treatment for thinking: a multi-message reply that
            # spans multiple OSS rows still gets one consolidated reasoning
            # block in the admin view. Empty rows (older outbound messages
            # persisted before OSS migration 033 ran) are filtered so we
            # don't render stray separators.
            joined_thinking = "\n\n".join(
                m.thinking_text for m in pending_agent_msgs if m.thinking_text
            )
            agent_reply = SharedDataMessageItem(
                seq=last.seq,
                direction="outbound",
                body=redact_pii(joined_body),
                thinking=redact_pii(joined_thinking),
                timestamp=last.timestamp.isoformat() if last.timestamp else None,
            )
        user_message: SharedDataMessageItem | None = None
        started_at: str | None = None
        if pending_user is not None:
            user_message = SharedDataMessageItem(
                seq=pending_user.seq,
                direction="inbound",
                body=redact_pii(pending_user.body or ""),
                timestamp=pending_user.timestamp.isoformat() if pending_user.timestamp else None,
            )
            started_at = user_message.timestamp
        elif pending_agent_msgs:
            first = pending_agent_msgs[0]
            started_at = first.timestamp.isoformat() if first.timestamp else None
        finished_at = agent_reply.timestamp if agent_reply else started_at

        turns.append(
            SharedDataTurn(
                turn_index=turn_index,
                user_message=user_message,
                agent_reply=agent_reply,
                tool_calls=list(pending_tools),
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        turn_index += 1
        pending_user = None
        pending_agent_msgs = []
        pending_tools = []

    for msg in messages:
        if msg.direction == "inbound":
            flush()
            pending_user = msg
        else:  # outbound
            pending_agent_msgs.append(msg)
            for interaction in _parse_tool_interactions(msg.tool_interactions_json):
                pending_tools.append(_redact_tool_call(interaction))

    flush()
    return turns


@router.get(
    "/users/{user_id}/conversation/turns",
    response_model=SharedDataConversationTurnsResponse,
)
async def list_shared_data_conversation_turns(
    user_id: str,
    limit: int = Query(500, ge=1, le=2000),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_CONVERSATION_TURNS)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataConversationTurnsResponse:
    """Return the user's conversation as turn-grouped, redacted records.

    Pulls every message in the user's single conversation (capped at
    ``limit`` rows so a runaway transcript does not OOM the response),
    groups them into turns via :func:`_group_turns`, and returns each
    turn with its user message, agent reply, and the tool calls fired
    during the turn. Each tool call is redacted at the leaves: ``args``
    is walked recursively and ``result`` is string-redacted, so a
    query like ``qb_query("...WHERE customer_name='John Smith'")``
    does not surface the customer name even when the conversation is
    opened.

    The consent gate is re-checked server-side, so a user revoking
    consent mid-investigation immediately starts returning 403.
    """
    user = await _require_consenting_user(db, user_id)
    session = (
        await db.execute(select(ChatSession).where(ChatSession.user_id == user.id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="No conversation yet")

    ctx.target_user_id = user.id
    ctx.resource_type = "conversation"
    ctx.resource_id = session.session_id

    # Order DESC + reverse so the limit clips OLDEST messages, not most
    # recent. Trimmed messages stay in the DB after compaction (only
    # `last_trim_seq` advances), so on a long-running conversation an
    # ASC + limit query burns its budget on history the admin already
    # cannot use for context and silently chops off the live tail
    # admins actually opened the page to see.
    recent = list(
        (
            await db.execute(
                select(Message)
                .where(Message.session_id == session.id)
                .order_by(Message.seq.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    messages = list(reversed(recent))

    turns = _group_turns(messages)
    return SharedDataConversationTurnsResponse(
        session_id=session.session_id,
        user_id=user.id,
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        turns=turns,
        total=len(turns),
        last_trim_seq=session.last_trim_seq,
    )


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
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
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
    skipped — often quotes the user back to themselves), and ``tasks``
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
    rows = (
        (
            await db.execute(
                base_stmt.order_by(HeartbeatLog.created_at.desc().nullslast()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        SharedDataHeartbeatLogItem(
            id=row.id,
            action_type=row.action_type or "",
            channel=row.channel or "",
            message_text=redact_pii(row.message_text or ""),
            reasoning=redact_pii(row.reasoning or ""),
            tasks=redact_pii(row.tasks or ""),
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in rows
    ]
    total = (await db.execute(count_stmt)).scalar_one() or 0
    return SharedDataHeartbeatLogListResponse(
        user_id=user.id,
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
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
    strings rather than 404 — consenting and "no memory yet" is a
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
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        memory_text=redact_pii(doc.memory_text if doc else ""),
        history_text=redact_pii(doc.history_text if doc else ""),
        updated_at=(doc.updated_at.isoformat() if doc and doc.updated_at else None),
    )


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
    rows = (
        (
            await db.execute(
                base_stmt.order_by(CompactionEvent.triggered_at.desc().nullslast()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [
        SharedDataCompactionEventItem(
            id=row.id,
            triggered_at=row.triggered_at.isoformat() if row.triggered_at else None,
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
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        items=items,
        total=int(total),
    )


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
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in records
    ]
    return SharedDataApprovalEventListResponse(
        user_id=user.id,
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        items=items,
        total=len(items),
    )


@router.get(
    "/users/{user_id}/export",
    response_model=SharedDataExportResponse,
)
async def export_shared_data_user(
    user_id: str,
    days: int = Query(
        7,
        ge=1,
        le=90,
        description=(
            "Window size in days, ending now. Scopes the time-bucketed "
            "subresources (conversations, heartbeat-logs, compaction-events, "
            "LLM-usage, reports, and the tool-call rollup). Identity, "
            "profile, memory, and the configured-directives count "
            "(``heartbeat_directives_count``) are point-in-time and not "
            "scoped by the window."
        ),
    ),
    include_turns: bool = Query(
        False,
        description=(
            "When true, attach turn-grouped transcripts for every conversation in "
            "the window. Off by default because turns are expensive both to "
            "compute and to ship; flip on when you want full body content."
        ),
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_SHARED_DATA_EXPORT)),
    db: AsyncSession = Depends(get_async_db),
) -> SharedDataExportResponse:
    """One-shot export bundle for a consenting user.

    Designed for CLI / offline analysis: a single audit-logged request
    returns identity + profile + memory + heartbeat history + activity
    counts + tool usage rollup, all PII-redacted. Without this, an
    admin investigating "what's wrong with this user's experience?"
    has to walk seven separate endpoints by hand.

    The window only applies to time-bucketed sub-resources. The
    profile / memory / consent fields are always the current value.

    Bodies live in two places:
    * The agent's persistent text (soul, user, heartbeat directives,
      memory) returns in the corresponding sections.
    * Per-message transcripts only return when ``include_turns=true``.
      They are the heaviest field (one row per message + tool call).
    """
    user = await _require_consenting_user(db, user_id)
    ctx.target_user_id = user.id
    ctx.resource_type = "user"
    ctx.resource_id = user.id

    now = datetime.now(tz=UTC)
    window_start = now - timedelta(days=days)
    ctx.detail = {"days": days, "include_turns": include_turns}

    # ``last_login_at`` lives on the ``users`` table (added via the
    # multi-user column set) but is not exposed as a mapped
    # ORM attribute on the OSS ``User`` class. Read the column directly
    # like the admin_router does for the same field.
    last_login_col = User.__table__.c.last_login_at
    last_login_row = (await db.execute(select(last_login_col).where(User.id == user.id))).first()
    last_login_iso: str | None = (
        last_login_row[0].isoformat() if last_login_row and last_login_row[0] is not None else None
    )

    # ---- counts: sessions / messages ---------------------------------
    sessions = (
        (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.user_id == user.id,
                    ChatSession.last_message_at >= window_start,
                )
                .order_by(ChatSession.last_message_at.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    session_count = len(sessions)
    session_ids = [s.id for s in sessions]

    if session_ids:
        msg_dir_rows = (
            await db.execute(
                select(Message.direction, sa_func.count(Message.id))
                .where(
                    Message.session_id.in_(session_ids),
                    Message.timestamp >= window_start,
                )
                .group_by(Message.direction)
            )
        ).all()
    else:
        msg_dir_rows = []
    inbound_count = 0
    outbound_count = 0
    for direction, count in msg_dir_rows:
        if direction == "inbound":
            inbound_count = int(count)
        elif direction == "outbound":
            outbound_count = int(count)
    message_count = inbound_count + outbound_count

    # ---- counts: heartbeats by action_type ---------------------------
    hb_action_rows = (
        await db.execute(
            select(HeartbeatLog.action_type, sa_func.count(HeartbeatLog.id))
            .where(
                HeartbeatLog.user_id == user.id,
                HeartbeatLog.created_at >= window_start,
            )
            .group_by(HeartbeatLog.action_type)
        )
    ).all()
    heartbeats_by_action: dict[str, int] = {
        (action or "unknown"): int(count) for action, count in hb_action_rows
    }
    heartbeats_total = sum(heartbeats_by_action.values())

    # ---- counts: compactions -----------------------------------------
    compactions_count = (
        await db.execute(
            select(sa_func.count(CompactionEvent.id)).where(
                CompactionEvent.user_id == user.id,
                CompactionEvent.triggered_at >= window_start,
            )
        )
    ).scalar_one() or 0

    # ---- counts: LLM usage by purpose --------------------------------
    llm_rows = (
        await db.execute(
            select(
                LLMUsageLog.purpose,
                sa_func.count(LLMUsageLog.id),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.input_tokens), 0),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.output_tokens), 0),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.cache_read_input_tokens), 0),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.cost), 0),
            )
            .where(
                LLMUsageLog.user_id == user.id,
                LLMUsageLog.created_at >= window_start,
            )
            .group_by(LLMUsageLog.purpose)
        )
    ).all()
    llm_calls_by_purpose: dict[str, int] = {}
    llm_input = 0
    llm_output = 0
    llm_cache = 0
    llm_cost = 0.0
    llm_total = 0
    for purpose, n, in_t, out_t, cache_t, cost in llm_rows:
        key = purpose or "unknown"
        llm_calls_by_purpose[key] = int(n)
        llm_total += int(n)
        llm_input += int(in_t or 0)
        llm_output += int(out_t or 0)
        llm_cache += int(cache_t or 0)
        llm_cost += float(cost or 0)

    # ---- counts: tool calls ------------------------------------------
    # Walk the tool_interactions_json column on outbound messages in
    # the window. Inbound rows never carry tool calls (the column is
    # default empty for user-authored messages), but we filter
    # explicitly so a future change that ever populated it on inbound
    # rows wouldn't double-count. We do this in Python rather than SQL
    # so a single corrupt JSON row does not blow up the whole export;
    # _parse_tool_interactions already drops invalid entries.
    tool_calls_total = 0
    tool_calls_error_count = 0
    tool_call_counts: dict[str, int] = {}
    tool_error_counts: dict[str, int] = {}
    if session_ids:
        rows = (
            await db.execute(
                select(Message.tool_interactions_json).where(
                    Message.session_id.in_(session_ids),
                    Message.direction == "outbound",
                    Message.timestamp >= window_start,
                    Message.tool_interactions_json.is_not(None),
                )
            )
        ).all()
        for (raw,) in rows:
            for interaction in _parse_tool_interactions(raw or ""):
                tool_calls_total += 1
                name = interaction.name or "unknown"
                tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
                if interaction.is_error:
                    tool_calls_error_count += 1
                    tool_error_counts[name] = tool_error_counts.get(name, 0) + 1

    tool_calls_top = [
        SharedDataExportTopTool(
            name=name,
            call_count=count,
            error_count=tool_error_counts.get(name, 0),
        )
        for name, count in sorted(tool_call_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]

    # ---- counts: reports + heartbeat directives ----------------------
    # Scope reports to the same window as the rest of the time-bucketed
    # subresources so a user with a long history does not show one
    # cumulative ``reports_total`` next to a windowed
    # ``heartbeats_total`` and ``message_count``. Cumulative report
    # totals are still reachable via the dedicated
    # ``/admin/reported-conversations`` endpoints.
    reports_total = (
        await db.execute(
            select(sa_func.count(ReportedConversation.id)).where(
                ReportedConversation.user_id == user.id,
                ReportedConversation.created_at >= window_start,
            )
        )
    ).scalar_one() or 0
    # heartbeat_items has no OSS ORM model yet; one COUNT via raw SQL.
    # Point-in-time, not windowed: this is "how many directives are
    # configured right now?", which is the load-bearing question for
    # debugging "why is the agent never proactive?". A windowed count
    # would mean "directives created in the last N days" which is a
    # weaker signal. See SharedDataExportSummary.heartbeat_directives_count.
    # Defensive: the test DB uses create_all() against ORM models, so
    # the table is absent there. Wrap in a SAVEPOINT so the failure
    # rolls back ONLY the count probe, leaving the outer transaction
    # (and our already-loaded ``user`` row) intact. The count is
    # informational, not load-bearing.
    hb_dir_count = 0
    try:
        async with db.begin_nested():
            hb_dir_count = (
                await db.execute(
                    text("SELECT COUNT(*) FROM heartbeat_items WHERE user_id = :uid"),
                    {"uid": user.id},
                )
            ).scalar() or 0
    except Exception:
        hb_dir_count = 0

    summary = SharedDataExportSummary(
        session_count=session_count,
        message_count=message_count,
        inbound_count=inbound_count,
        outbound_count=outbound_count,
        heartbeats_total=heartbeats_total,
        heartbeats_by_action=heartbeats_by_action,
        compactions_count=int(compactions_count),
        llm_calls_total=llm_total,
        llm_calls_by_purpose=llm_calls_by_purpose,
        llm_cost_usd=f"{llm_cost:.6f}",
        llm_input_tokens=llm_input,
        llm_output_tokens=llm_output,
        llm_cache_read_tokens=llm_cache,
        tool_calls_total=tool_calls_total,
        tool_calls_error_count=tool_calls_error_count,
        tool_calls_top=tool_calls_top,
        reports_total=int(reports_total),
        heartbeat_directives_count=int(hb_dir_count),
    )

    # ---- per-session conversation list (no bodies) -------------------
    if session_ids:
        msg_count_rows = (
            await db.execute(
                select(Message.session_id, sa_func.count(Message.id))
                .where(Message.session_id.in_(session_ids))
                .group_by(Message.session_id)
            )
        ).all()
    else:
        msg_count_rows = []
    msg_count_by_session = {sid: int(c) for sid, c in msg_count_rows}
    conversations = [
        SharedDataConversationItem(
            session_id=s.session_id,
            channel=s.channel or "",
            created_at=s.created_at.isoformat() if s.created_at else None,
            last_message_at=s.last_message_at.isoformat() if s.last_message_at else None,
            message_count=msg_count_by_session.get(s.id, 0),
            last_trim_seq=s.last_trim_seq,
        )
        for s in sessions
    ]

    # ---- heartbeat logs (PII-redacted content) -----------------------
    hb_rows = (
        (
            await db.execute(
                select(HeartbeatLog)
                .where(
                    HeartbeatLog.user_id == user.id,
                    HeartbeatLog.created_at >= window_start,
                )
                .order_by(HeartbeatLog.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    heartbeat_logs = [
        SharedDataHeartbeatLogItem(
            id=row.id,
            action_type=row.action_type or "",
            channel=row.channel or "",
            message_text=redact_pii(row.message_text or ""),
            reasoning=redact_pii(row.reasoning or ""),
            tasks=redact_pii(row.tasks or ""),
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in hb_rows
    ]

    # ---- compaction events -------------------------------------------
    compaction_rows = (
        (
            await db.execute(
                select(CompactionEvent)
                .where(
                    CompactionEvent.user_id == user.id,
                    CompactionEvent.triggered_at >= window_start,
                )
                .order_by(CompactionEvent.triggered_at.desc())
            )
        )
        .scalars()
        .all()
    )
    compaction_events = [
        SharedDataCompactionEventItem(
            id=row.id,
            triggered_at=row.triggered_at.isoformat() if row.triggered_at else None,
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
        for row in compaction_rows
    ]

    # ---- profile + memory --------------------------------------------
    profile = SharedDataProfileResponse(
        user_id=user.id,
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        soul_text=redact_pii(user.soul_text or ""),
        user_text=redact_pii(user.user_text or ""),
        heartbeat_text=redact_pii(user.heartbeat_text or ""),
        heartbeat_opt_in=bool(user.heartbeat_opt_in),
        heartbeat_frequency=user.heartbeat_frequency or "",
        heartbeat_max_daily=int(user.heartbeat_max_daily or 0),
    )

    mem_row = (
        await db.execute(select(MemoryDocument).where(MemoryDocument.user_id == user.id))
    ).scalar_one_or_none()
    memory = SharedDataMemoryDocumentResponse(
        user_id=user.id,
        consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
        memory_text=redact_pii(mem_row.memory_text if mem_row and mem_row.memory_text else ""),
        history_text=redact_pii(mem_row.history_text if mem_row and mem_row.history_text else ""),
        updated_at=(mem_row.updated_at.isoformat() if mem_row and mem_row.updated_at else None),
    )

    # ---- optional: turn-grouped transcripts --------------------------
    # One bulk query for every message across every session in the
    # window, then bucket in Python. The previous shape ran one
    # SELECT per session (N+1); for a user with hundreds of
    # conversations under include_turns=true that was a real cost.
    turns_payload: list[SharedDataConversationTurnsResponse] | None = None
    if include_turns and session_ids:
        all_messages = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id.in_(session_ids))
                    .order_by(Message.session_id.asc(), Message.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        messages_by_session: dict[int, list[Message]] = {}
        for msg in all_messages:
            messages_by_session.setdefault(msg.session_id, []).append(msg)
        turns_payload = []
        for sess in sessions:
            grouped = _group_turns(messages_by_session.get(sess.id, []))
            turns_payload.append(
                SharedDataConversationTurnsResponse(
                    session_id=sess.session_id,
                    user_id=user.id,
                    consent_at=(
                        user.data_sharing_consent_at.isoformat()
                        if user.data_sharing_consent_at
                        else None
                    ),
                    turns=grouped,
                    total=len(grouped),
                )
            )

    user_subs = (
        (await db.execute(select(Subscription).where(Subscription.user_id == user.id)))
        .scalars()
        .all()
    )
    user_email = next((sub.email for sub in user_subs), "")

    return SharedDataExportResponse(
        user_id=user.id,
        user={
            "user_id": user.user_id,
            "email": user_email,
            "is_active": bool(user.is_active),
            "onboarding_complete": bool(user.onboarding_complete),
            "timezone": user.timezone or "",
            "preferred_channel": user.preferred_channel or "",
            "data_sharing_consent_at": (
                user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
            ),
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": last_login_iso,
        },
        window={
            "start": window_start.isoformat(),
            "end": now.isoformat(),
            "days": days,
        },
        summary=summary,
        conversations=conversations,
        heartbeat_logs=heartbeat_logs,
        compaction_events=compaction_events,
        profile=profile,
        memory=memory,
        turns=turns_payload,
    )
