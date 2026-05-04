"""Endpoints for the user's conversation.

Each user has at most one conversation; these endpoints expose it
without requiring the caller to know a session ID. The conversation
row is created on the user's first message via the agent pipeline,
not by these endpoints; ``GET /user/conversation`` returns an empty
shape until then.
"""

import contextlib
import json

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.concurrency import user_locks
from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.onboarding import build_onboarding_system_prompt, is_in_onboarding_flow
from backend.app.agent.session_db import get_session_store
from backend.app.agent.system_prompt import build_agent_system_prompt
from backend.app.agent.tool_assembly import build_initial_turn_tools
from backend.app.agent.tool_summary import append_receipts
from backend.app.auth.dependencies import get_current_user
from backend.app.database import SessionLocal
from backend.app.models import ChatSession, User
from backend.app.schemas import (
    BatchDeleteRequest,
    DeleteMessageResponse,
    DeleteMessagesResponse,
    SessionDetailResponse,
    SessionMessage,
    SessionSystemPromptResponse,
)

router = APIRouter()


def _user_session_id(user_id: str) -> str | None:
    """Return the user's session_id, or None if they have no conversation yet."""
    db = SessionLocal()
    try:
        cs = db.query(ChatSession).filter_by(user_id=user_id).first()
        return cs.session_id if cs is not None else None
    finally:
        db.close()


def _empty_conversation(user_id: str) -> SessionDetailResponse:
    return SessionDetailResponse(
        session_id="",
        user_id=user_id,
        created_at="",
        last_message_at="",
        channel="",
        initial_system_prompt="",
        messages=[],
    )


@router.get("/user/conversation", response_model=SessionDetailResponse)
async def get_conversation(
    current_user: User = Depends(get_current_user),
) -> SessionDetailResponse:
    """Return the user's conversation transcript, or an empty shape if none yet.

    Returning an empty shape (rather than 404) lets the frontend render
    the chat input without special-casing the first-message-ever flow.
    The session row is created by the agent pipeline on the first
    inbound message, not by this endpoint.
    """
    session_id = _user_session_id(current_user.id)
    if session_id is None:
        return _empty_conversation(current_user.id)

    store = get_session_store(current_user.id)
    session = store.load_session(session_id)
    if session is None:
        # Race: session row was deleted between the queries above.
        return _empty_conversation(current_user.id)

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
        channel=session.channel,
        initial_system_prompt=session.initial_system_prompt,
        messages=messages,
    )


@router.get(
    "/user/conversation/system-prompt",
    response_model=SessionSystemPromptResponse,
)
async def get_conversation_system_prompt(
    current_user: User = Depends(get_current_user),
) -> SessionSystemPromptResponse:
    """Return the system prompt that would be sent on the next turn.

    Reconstructed live from current user state (profile, soul, memory,
    onboarding status, tool availability) so the UI doesn't show a
    stale snapshot from the first turn of the session.

    Known approximations:

    * The preview omits specialist tool guidelines that get appended
      mid-turn when the LLM calls ``list_capabilities`` to activate a
      category. It matches the start-of-turn tool list, mirroring how
      the agent itself starts each turn fresh.
    * Tools whose factories require a storage backend or an outbound
      publish hook (currently ``send_media_reply``,
      ``upload_to_storage``, and ``organize_file``) are filtered out
      by the registry's dependency gates because the preview can't
      safely construct those runtime hooks. Their usage hints will
      not appear in the Tool Guidelines section.
    * If a user's ``BOOTSTRAP.md`` cannot be created on disk by the
      runtime (rare, requires an OS-level error), the runtime drops
      out of onboarding mode while this preview still reports
      ``is_onboarding=true`` based on the in-memory heuristic.
    """
    session_id = _user_session_id(current_user.id)
    if session_id is None:
        raise HTTPException(status_code=404, detail="No conversation yet")
    store = get_session_store(current_user.id)
    session = store.load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No conversation yet")

    is_onboarding = is_in_onboarding_flow(current_user)
    tools = await build_initial_turn_tools(current_user, channel=session.channel)

    if is_onboarding:
        system_prompt = build_onboarding_system_prompt(current_user, tools=tools)
    else:
        # build_memory_section currently ignores its query parameter
        # (it returns the full MEMORY.md), so we pass an empty string
        # rather than scanning session messages for a "best query" that
        # would be discarded. If memory ever becomes query-driven, the
        # endpoint should pass a real query (e.g. the last inbound
        # message body) so the preview reflects what the next turn
        # would retrieve.
        system_prompt = await build_agent_system_prompt(
            current_user,
            tools,
            message_context="",
            current_session_id=session.session_id,
        )

    return SessionSystemPromptResponse(
        session_id=session.session_id,
        system_prompt=system_prompt,
        is_onboarding=is_onboarding,
    )


@router.delete(
    "/user/conversation/messages/batch",
    response_model=DeleteMessagesResponse,
)
async def delete_messages_batch(
    body: BatchDeleteRequest,
    current_user: User = Depends(get_current_user),
) -> DeleteMessagesResponse:
    """Delete specific messages from the user's conversation by sequence number."""
    session_id = _user_session_id(current_user.id)
    if session_id is None:
        raise HTTPException(status_code=404, detail="No conversation yet")
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        deleted = store.delete_messages_by_seqs(session_id, body.seqs)
    return DeleteMessagesResponse(status="deleted", messages_deleted=deleted)


@router.delete(
    "/user/conversation/messages/{seq}",
    response_model=DeleteMessageResponse,
)
async def delete_single_message(
    seq: int,
    current_user: User = Depends(get_current_user),
) -> DeleteMessageResponse:
    """Delete a single message from the user's conversation by sequence number."""
    session_id = _user_session_id(current_user.id)
    if session_id is None:
        raise HTTPException(status_code=404, detail="No conversation yet")
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        deleted = store.delete_message(session_id, seq)
        if not deleted:
            raise HTTPException(status_code=404, detail="Message not found")
    return DeleteMessageResponse(status="deleted", seq=seq)


@router.delete(
    "/user/conversation/messages",
    response_model=DeleteMessagesResponse,
)
async def delete_conversation_history(
    current_user: User = Depends(get_current_user),
) -> DeleteMessagesResponse:
    """Delete all messages from the user's conversation, preserving memory.

    Resets the initial system prompt so the conversation continues with
    a clean slate while retaining compacted memory.
    """
    session_id = _user_session_id(current_user.id)
    if session_id is None:
        raise HTTPException(status_code=404, detail="No conversation yet")
    store = get_session_store(current_user.id)
    async with user_locks.acquire(current_user.id):
        deleted = store.delete_messages(session_id)
    return DeleteMessagesResponse(status="deleted", messages_deleted=deleted)
