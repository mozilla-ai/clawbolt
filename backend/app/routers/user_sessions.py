"""Endpoints for viewing conversation sessions."""

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from backend.app.agent.concurrency import user_locks
from backend.app.agent.session_db import get_session_store
from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_db
from backend.app.models import ChatSession, Message, User
from backend.app.schemas import (
    DeleteMessagesResponse,
    SessionDetailResponse,
    SessionListItem,
    SessionListResponse,
    SessionMessage,
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
        messages.append(
            SessionMessage(
                seq=msg.seq,
                direction=msg.direction,
                body=msg.body,
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
