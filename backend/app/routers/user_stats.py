"""Endpoint for user overview stats."""

import datetime

from fastapi import APIRouter, Depends

from backend.app.agent.file_store import (
    HeartbeatStore,
    UserData,
    get_memory_store,
    get_session_store,
)
from backend.app.auth.dependencies import get_current_user
from backend.app.enums import HeartbeatStatus
from backend.app.schemas import UserStatsResponse

router = APIRouter()


@router.get("/user/stats", response_model=UserStatsResponse)
async def get_stats(
    current_user: UserData = Depends(get_current_user),
) -> UserStatsResponse:
    """Return aggregate stats for the dashboard overview."""
    session_store = get_session_store(current_user.id)
    memory_store = get_memory_store(current_user.id)
    heartbeat_store = HeartbeatStore(current_user.id)

    # Total sessions
    session_files = session_store._list_session_files()
    total_sessions = len(session_files)

    # Messages this month
    now = datetime.datetime.now(datetime.UTC)
    month_prefix = now.strftime("%Y-%m")
    messages_this_month = 0
    last_conversation_at: str | None = None

    for path in session_files:
        session = session_store._load_session(path.stem)
        if session is None:
            continue
        if session.last_message_at and (
            last_conversation_at is None or session.last_message_at > last_conversation_at
        ):
            last_conversation_at = session.last_message_at
        for msg in session.messages:
            if msg.timestamp.startswith(month_prefix):
                messages_this_month += 1

    # Active heartbeat items
    heartbeat_items = await heartbeat_store.get_heartbeat_items()
    active_heartbeat_items = sum(
        1 for item in heartbeat_items if item.status == HeartbeatStatus.ACTIVE
    )

    # Memory: count non-empty lines as a rough "facts" proxy
    memory_text = memory_store.read_memory()
    total_memory_facts = (
        sum(1 for line in memory_text.splitlines() if line.strip() and not line.startswith("#"))
        if memory_text
        else 0
    )

    return UserStatsResponse(
        total_sessions=total_sessions,
        messages_this_month=messages_this_month,
        active_heartbeat_items=active_heartbeat_items,
        total_memory_facts=total_memory_facts,
        last_conversation_at=last_conversation_at,
    )
