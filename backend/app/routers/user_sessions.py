"""Endpoints for viewing conversation sessions."""

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.session_db import get_session_store
from backend.app.auth.dependencies import get_current_user
from backend.app.models import User
from backend.app.schemas import (
    SessionDetailResponse,
    SessionMessage,
)

router = APIRouter()


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
        messages=messages,
    )
