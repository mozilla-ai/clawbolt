"""Endpoints for viewing conversation sessions."""

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from backend.app.agent.concurrency import user_locks
from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.onboarding import build_onboarding_system_prompt, is_onboarding_needed
from backend.app.agent.session_db import get_session_store
from backend.app.agent.system_prompt import build_agent_system_prompt
from backend.app.agent.tool_assembly import build_initial_turn_tools
from backend.app.agent.tool_summary import append_receipts
from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User
from backend.app.schemas import (
    BatchDeleteRequest,
    DeleteMessageResponse,
    DeleteMessagesResponse,
    SessionDetailResponse,
    SessionListItem,
    SessionListResponse,
    SessionMessage,
    SessionSystemPromptResponse,
)

router = APIRouter()


@router.get("/user/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    is_active: bool | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionListResponse:
    """List sessions with message counts, ordered by last_message_at DESC."""
    base_filter = [ChatSession.user_id == current_user.id]
    if is_active is not None:
        base_filter.append(ChatSession.is_active == is_active)

    total: int = (db.query(sa_func.count(ChatSession.id)).filter(*base_filter).scalar()) or 0

    # Subquery for message count
    msg_count = sa_func.count(Message.id).label("message_count")
    rows = (
        db.query(ChatSession, msg_count)
        .outerjoin(Message, Message.session_id == ChatSession.id)
        .filter(*base_filter)
        .group_by(ChatSession.id)
        .order_by(ChatSession.last_message_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = [
        SessionListItem(
            session_id=cs.session_id,
            channel=cs.channel or "",
            is_active=cs.is_active,
            message_count=count,
            created_at=cs.created_at.isoformat() if cs.created_at else "",
            last_message_at=cs.last_message_at.isoformat() if cs.last_message_at else "",
        )
        for cs, count in rows
    ]

    return SessionListResponse(total=total, items=items)


@router.get("/user/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionDetailResponse:
    """Get a full conversation transcript with tool interactions."""
    store = get_session_store(current_user.id)
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    messages: list[SessionMessage] = []
    for msg in session.messages:
        tool_interactions: list[dict[str, object]] = []
        if msg.tool_interactions_json and msg.tool_interactions_json not in ("", "[]"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                tool_interactions = json.loads(msg.tool_interactions_json)

        # Mirror the channel-side transform: outbound replies render with the
        # deterministic receipt block appended, matching what iMessage/Telegram
        # users receive. Messages in the DB store the raw LLM reply so the
        # agent's own history stays clean; the receipt block is recomputed
        # here for the display surface.
        body = msg.body
        if msg.direction == "outbound" and tool_interactions:
            stored: list[StoredToolInteraction] = []
            for entry in tool_interactions:
                with contextlib.suppress(Exception):
                    stored.append(StoredToolInteraction.model_validate(entry))
            if stored:
                body = append_receipts(body, stored)

        messages.append(
            SessionMessage(
                seq=msg.seq,
                direction=msg.direction,
                body=body,
                timestamp=msg.timestamp,
                tool_interactions=tool_interactions,
            )
        )

    return SessionDetailResponse(
        session_id=session.session_id,
        user_id=session.user_id,
        created_at=session.created_at,
        last_message_at=session.last_message_at,
        is_active=session.is_active,
        channel=session.channel,
        initial_system_prompt=session.initial_system_prompt,
        last_compacted_seq=session.last_compacted_seq,
        messages=messages,
    )


@router.get(
    "/user/sessions/{session_id}/system-prompt",
    response_model=SessionSystemPromptResponse,
)
async def get_session_system_prompt(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> SessionSystemPromptResponse:
    """Return the system prompt that would be sent on the next turn.

    Reconstructed live from current user state (profile, soul, memory,
    onboarding status, tool availability) so the UI doesn't show a
    stale snapshot from the first turn of the session. The prompt
    matches what the agent will actually send the next time the user
    posts a message in this session, modulo specialist tool guidelines
    that get appended mid-turn when ``list_capabilities`` activates a
    category.
    """
    store = get_session_store(current_user.id)
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Memory retrieval is query-driven, so use the most recent inbound
    # message as the query. Without a query the memory section degrades
    # to an empty list, which under-represents what the next turn will
    # see if the user follows up on the same topic.
    message_context = ""
    for msg in reversed(session.messages):
        if msg.direction == MessageDirection.INBOUND and msg.body:
            message_context = msg.body
            break

    is_onboarding = is_onboarding_needed(current_user)
    tools = await build_initial_turn_tools(current_user, channel=session.channel or None)

    if is_onboarding:
        system_prompt = build_onboarding_system_prompt(current_user, tools=tools)
    else:
        system_prompt = await build_agent_system_prompt(
            current_user,
            tools,
            message_context,
            current_session_id=session.session_id,
        )

    return SessionSystemPromptResponse(
        session_id=session.session_id,
        system_prompt=system_prompt,
        is_onboarding=is_onboarding,
    )


@router.delete(
    "/user/sessions/{session_id}/messages/batch",
    response_model=DeleteMessagesResponse,
)
async def delete_messages_batch(
    session_id: str,
    body: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
) -> DeleteMessagesResponse:
    """Delete specific messages from a session by their sequence numbers."""
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        deleted = store.delete_messages_by_seqs(session_id, body.seqs)
    return DeleteMessagesResponse(status="deleted", messages_deleted=deleted)


@router.delete(
    "/user/sessions/{session_id}/messages/{seq}",
    response_model=DeleteMessageResponse,
)
async def delete_single_message(
    session_id: str,
    seq: int,
    current_user: User = Depends(get_current_user),
) -> DeleteMessageResponse:
    """Delete a single message from a session by its sequence number."""
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        deleted = store.delete_message(session_id, seq)
        if not deleted:
            raise HTTPException(status_code=404, detail="Message not found")
    return DeleteMessageResponse(status="deleted", seq=seq)


@router.delete(
    "/user/sessions/{session_id}/messages",
    response_model=DeleteMessagesResponse,
)
async def delete_conversation_history(
    session_id: str,
    current_user: User = Depends(get_current_user),
) -> DeleteMessagesResponse:
    """Delete all messages from a session, preserving memory and the session itself.

    Resets the compaction pointer and system prompt so the conversation
    continues with a clean slate while retaining compacted memory.
    """
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        session = store.load_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        deleted = store.delete_messages(session_id)
    return DeleteMessagesResponse(status="deleted", messages_deleted=deleted)
