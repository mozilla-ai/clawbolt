"""Endpoint for contractor overview stats."""

import datetime

from fastapi import APIRouter, Depends

from backend.app.agent.file_store import (
    ContractorData,
    HeartbeatStore,
    get_memory_store,
    get_session_store,
)
from backend.app.auth.dependencies import get_current_user
from backend.app.enums import ChecklistStatus
from backend.app.schemas import ContractorStatsResponse

router = APIRouter()


@router.get("/user/stats", response_model=ContractorStatsResponse)
async def get_stats(
    current_user: ContractorData = Depends(get_current_user),
) -> ContractorStatsResponse:
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

    # Active checklist items
    checklist = await heartbeat_store.get_checklist()
    active_checklist_items = sum(1 for item in checklist if item.status == ChecklistStatus.ACTIVE)

    # Total memory facts
    facts = await memory_store.get_all_memories()
    total_memory_facts = len(facts)

    return ContractorStatsResponse(
        total_sessions=total_sessions,
        messages_this_month=messages_this_month,
        active_checklist_items=active_checklist_items,
        total_memory_facts=total_memory_facts,
        last_conversation_at=last_conversation_at,
    )
