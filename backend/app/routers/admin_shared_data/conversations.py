"""Conversation transcripts, grouped into turns and redacted at serialization."""

from __future__ import annotations

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.context import StoredToolInteraction
from backend.app.database import get_async_db
from backend.app.models import (
    ChatSession,
    Message,
)
from backend.app.query_helpers import iso_or_none
from backend.app.routers.admin_shared_data.consent import _require_consenting_user
from backend.app.schemas.shared_data import (
    SharedDataConversationItem,
    SharedDataConversationTurnsResponse,
    SharedDataMessageItem,
    SharedDataReceipt,
    SharedDataToolCall,
    SharedDataTurn,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.pii_redaction import redact_pii, redact_pii_recursive

router = APIRouter()


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
        created_at=iso_or_none(session.created_at),
        last_message_at=iso_or_none(session.last_message_at),
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
                timestamp=iso_or_none(last.timestamp),
            )
        user_message: SharedDataMessageItem | None = None
        started_at: str | None = None
        if pending_user is not None:
            user_message = SharedDataMessageItem(
                seq=pending_user.seq,
                direction="inbound",
                body=redact_pii(pending_user.body or ""),
                timestamp=iso_or_none(pending_user.timestamp),
            )
            started_at = user_message.timestamp
        elif pending_agent_msgs:
            first = pending_agent_msgs[0]
            started_at = iso_or_none(first.timestamp)
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
        consent_at=iso_or_none(user.data_sharing_consent_at),
        turns=turns,
        total=len(turns),
        last_trim_seq=session.last_trim_seq,
    )
