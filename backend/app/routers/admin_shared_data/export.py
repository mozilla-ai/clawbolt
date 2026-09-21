"""One-shot export bundle covering everything a consenting user has shared."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from backend.app.query_helpers import count_rows, fetch_all, iso_or_none
from backend.app.routers.admin_shared_data.compaction import _decode_snapshot
from backend.app.routers.admin_shared_data.consent import _require_consenting_user
from backend.app.routers.admin_shared_data.conversations import (
    _group_turns,
    _parse_tool_interactions,
)
from backend.app.schemas.shared_data import (
    SharedDataCompactionEventItem,
    SharedDataConversationItem,
    SharedDataConversationTurnsResponse,
    SharedDataExportResponse,
    SharedDataExportSummary,
    SharedDataExportTopTool,
    SharedDataHeartbeatLogItem,
    SharedDataMemoryDocumentResponse,
    SharedDataProfileResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii

router = APIRouter()


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
            "profile, and memory are point-in-time and not scoped by the "
            "window."
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
    sessions = await fetch_all(
        db,
        select(ChatSession)
        .where(
            ChatSession.user_id == user.id,
            ChatSession.last_message_at >= window_start,
        )
        .order_by(ChatSession.last_message_at.desc().nullslast()),
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
    compactions_count = await count_rows(
        db,
        CompactionEvent.id,
        CompactionEvent.user_id == user.id,
        CompactionEvent.triggered_at >= window_start,
    )

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
    reports_total = await count_rows(
        db,
        ReportedConversation.id,
        ReportedConversation.user_id == user.id,
        ReportedConversation.created_at >= window_start,
    )

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
            created_at=iso_or_none(s.created_at),
            last_message_at=iso_or_none(s.last_message_at),
            message_count=msg_count_by_session.get(s.id, 0),
            last_trim_seq=s.last_trim_seq,
        )
        for s in sessions
    ]

    # ---- heartbeat logs (PII-redacted content) -----------------------
    hb_rows = await fetch_all(
        db,
        select(HeartbeatLog)
        .where(
            HeartbeatLog.user_id == user.id,
            HeartbeatLog.created_at >= window_start,
        )
        .order_by(HeartbeatLog.created_at.desc()),
    )
    heartbeat_logs = [
        SharedDataHeartbeatLogItem(
            id=row.id,
            action_type=row.action_type or "",
            channel=row.channel or "",
            message_text=redact_pii(row.message_text or ""),
            reasoning=redact_pii(row.reasoning or ""),
            tasks=redact_pii(row.tasks or ""),
            created_at=iso_or_none(row.created_at),
        )
        for row in hb_rows
    ]

    # ---- compaction events -------------------------------------------
    compaction_rows = await fetch_all(
        db,
        select(CompactionEvent)
        .where(
            CompactionEvent.user_id == user.id,
            CompactionEvent.triggered_at >= window_start,
        )
        .order_by(CompactionEvent.triggered_at.desc()),
    )
    compaction_events = [
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
        for row in compaction_rows
    ]

    # ---- profile + memory --------------------------------------------
    profile = SharedDataProfileResponse(
        user_id=user.id,
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
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
        consent_at=(iso_or_none(user.data_sharing_consent_at)),
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
        all_messages = await fetch_all(
            db,
            select(Message)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id.asc(), Message.seq.asc()),
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
                    consent_at=(iso_or_none(user.data_sharing_consent_at)),
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
            "data_sharing_consent_at": (iso_or_none(user.data_sharing_consent_at)),
            "created_at": iso_or_none(user.created_at),
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
