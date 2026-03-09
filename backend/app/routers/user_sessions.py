"""Endpoints for viewing conversation sessions."""

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import UserData, get_session_store
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import (
    SessionDetailResponse,
    SessionListResponse,
    SessionMessage,
    SessionSummary,
)

router = APIRouter()


@router.get("/user/sessions", response_model=SessionListResponse)
async def list_sessions(
    offset: int = 0,
    limit: int = 20,
    current_user: UserData = Depends(get_current_user),
) -> SessionListResponse:
    """List conversation sessions for the current user."""
    store = get_session_store(current_user.id)
    files = store._list_session_files()
    # Most recent first
    files = list(reversed(files))
    total = len(files)
    page = files[offset : offset + limit]

    summaries: list[SessionSummary] = []
    for path in page:
        session_id = path.stem
        session = store._load_session(session_id)
        if session is None:
            continue
        preview = ""
        if session.messages:
            preview = session.messages[-1].body[:100]
        summaries.append(
            SessionSummary(
                id=session.session_id,
                start_time=session.created_at or session.last_message_at,
                message_count=len(session.messages),
                last_message_preview=preview,
            )
        )

    return SessionListResponse(
        sessions=summaries,
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/user/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    current_user: UserData = Depends(get_current_user),
) -> SessionDetailResponse:
    """Get a full conversation transcript with tool interactions."""
    store = get_session_store(current_user.id)
    session = store._load_session(session_id)
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
        messages=messages,
    )
