"""Conversation context loading and session management."""

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from backend.app.agent.compaction import compact_session
from backend.app.agent.dto import SessionState
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.session_db import SessionStore, get_session_store
from backend.app.config import settings
from backend.app.enums import MessageDirection

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = settings.conversation_history_limit

# Strong references to fire-and-forget background tasks so they are not
# garbage-collected before completion.
_background_tasks: set[asyncio.Task[None]] = set()


class StoredToolReceipt(BaseModel):
    """Schema for the optional ``ToolReceipt`` attached to a tool result.

    Write-side tools populate this so plain-text channels can render a
    deterministic, human-readable confirmation line tied to a real deep
    link from the API response.
    """

    action: str = ""
    target: str = ""
    url: str | None = None


class StoredToolInteraction(BaseModel):
    """Schema for tool interaction records stored in StoredMessage.tool_interactions_json."""

    tool_call_id: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    is_error: bool = False
    tags: set[str] = Field(default_factory=set, exclude=True)
    receipt: StoredToolReceipt | None = None


async def _run_compaction_in_background(
    session_store: SessionStore,
    session: SessionState,
    user_id: str,
    trimmed_agent_messages: list[AgentMessage],
    max_message_seq: int,
) -> None:
    """Run compaction and update the session's tracking field.

    This is designed to be fired as a background task via asyncio.create_task
    so it does not block message processing.
    """
    try:
        saved, compacted_seq = await compact_session(
            user_id, trimmed_agent_messages, max_message_seq=max_message_seq
        )
        if compacted_seq is not None:
            await session_store.update_compaction_seq(session, compacted_seq)
        if saved:
            logger.info(
                "Session compaction extracted %d fact(s) from %d trimmed message(s) for user %s",
                len(saved),
                len(trimmed_agent_messages),
                user_id,
            )
    except Exception:
        logger.exception(
            "Session compaction failed for session %s, user %s",
            session.session_id,
            user_id,
        )


def trigger_compaction_for_dropped(
    user_id: str,
    dropped_messages: list[AgentMessage],
) -> None:
    """Fire background compaction for messages that were trimmed from context.

    Called from the agent loop (``process_message``) when ``trim_messages``
    drops messages. The compaction task extracts durable facts from the
    dropped messages and stores them in MEMORY.md.

    Unlike the old count-based compaction in ``load_conversation_history``,
    the dropped messages here are ``AgentMessage`` objects without DB sequence
    numbers. We pass ``max_message_seq=None`` so ``compact_session`` extracts
    facts without advancing the compaction watermark. Session-end consolidation
    (``_consolidate_previous_session``) handles watermark tracking using real
    DB sequence values.
    """
    if not dropped_messages or not settings.compaction_enabled:
        return

    # Use compact_session directly (no seq tracking needed).
    # We don't need a session object since we're not updating compaction_seq.
    async def _run_trim_compaction() -> None:
        try:
            saved, _ = await compact_session(user_id, dropped_messages, max_message_seq=None)
            if saved:
                logger.info(
                    "Trim-based compaction extracted facts from %d dropped message(s) for user %s",
                    len(dropped_messages),
                    user_id,
                )
        except Exception:
            logger.exception(
                "Trim-based compaction failed for user %s",
                user_id,
            )

    task = asyncio.create_task(_run_trim_compaction())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    logger.info(
        "Triggered trim-based compaction for user %s: %d dropped message(s)",
        user_id,
        len(dropped_messages),
    )


def _parse_tool_interactions(raw: str) -> list[StoredToolInteraction]:
    """Parse tool_interactions_json, returning validated models.

    Each item is validated against ``StoredToolInteraction``. Missing fields
    receive defaults. Items that fail validation entirely are logged and
    skipped so corrupt data never crashes loading.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
    except (json.JSONDecodeError, TypeError):
        logger.debug("Could not parse tool_interactions_json, falling back to flat text")
        return []

    validated: list[StoredToolInteraction] = []
    for i, item in enumerate(parsed):
        try:
            validated.append(StoredToolInteraction.model_validate(item))
        except Exception:
            logger.warning(
                "Skipping invalid tool interaction record at index %d: %r",
                i,
                item,
            )
    return validated


def _expand_outbound_with_tools(
    tool_interactions: list[StoredToolInteraction],
    reply_text: str,
) -> list[AgentMessage]:
    """Expand an outbound message with tool interactions into typed messages.

    Reconstructs the message sequence the LLM originally produced:
    1. AssistantMessage with tool_calls (what the LLM requested)
    2. ToolResultMessage for each tool result
    3. AssistantMessage with the final reply text
    """
    messages: list[AgentMessage] = []

    # Build ToolCallRequest objects from the stored records
    tool_call_requests: list[ToolCallRequest] = []
    for tc in tool_interactions:
        tool_call_requests.append(
            ToolCallRequest(
                id=tc.tool_call_id,
                name=tc.name,
                arguments=tc.args,
            )
        )

    # AssistantMessage requesting the tool calls (content is typically None)
    messages.append(AssistantMessage(content=None, tool_calls=tool_call_requests))

    # ToolResultMessages for each tool execution
    for tc in tool_interactions:
        messages.append(
            ToolResultMessage(
                tool_call_id=tc.tool_call_id,
                content=tc.result,
            )
        )

    # Final AssistantMessage with the reply text
    messages.append(AssistantMessage(content=reply_text))

    return messages


async def load_conversation_history(
    session: SessionState,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[AgentMessage]:
    """Load recent messages as typed message objects for LLM context.

    Returns a list of typed messages in chronological order, excluding the
    most recent (which is the current message being processed).

    For outbound messages that have ``tool_interactions_json``, the full
    tool call/result sequence is reconstructed so the LLM can see its
    prior tool usage.  Messages without tool interaction data are loaded
    as flat ``AssistantMessage``.

    The *limit* parameter is a soft safety net that bounds memory usage
    (default 500). Token-based trimming in the agent loop is the primary
    guard against exceeding the LLM context window.
    """
    all_messages = session.messages
    total_count = len(all_messages)

    # Get the most recent `limit` messages, excluding the current (last) one
    if total_count > 1:
        messages = all_messages[-(limit):][:-1] if total_count > limit else all_messages[:-1]
    else:
        messages = []

    history: list[AgentMessage] = []
    tool_interaction_count = 0
    for msg in messages:
        # Prefer processed context (includes media descriptions) over raw body
        content = msg.processed_context if msg.processed_context else msg.body
        if msg.direction == MessageDirection.INBOUND:
            history.append(UserMessage(content=content))
        else:
            # Check for stored tool interactions
            tool_interactions = _parse_tool_interactions(msg.tool_interactions_json)
            if tool_interactions:
                tool_interaction_count += len(tool_interactions)
                history.extend(_expand_outbound_with_tools(tool_interactions, content))
            else:
                history.append(AssistantMessage(content=content))
    logger.debug(
        "Loaded %d history messages (%d with tool interactions) for session %s",
        len(history),
        tool_interaction_count,
        session.session_id,
    )
    return history


async def _consolidate_previous_session(
    session_store: SessionStore,
    user_id: str,
    current_session_id: str,
) -> None:
    """Consolidate unconsolidated messages from the most recent previous session.

    When a new session is created because the old one timed out, this function
    finds the previous session and runs compaction on any messages that were
    never compacted.  This ensures short conversations (that never overflowed
    the context window) still get their facts extracted and history logged.
    """
    for sid in reversed(session_store.list_session_ids()):
        if sid == current_session_id:
            continue
        prev = session_store.load_session(sid)
        if prev is None or not prev.messages:
            continue

        # Find unconsolidated messages
        unconsolidated = [m for m in prev.messages if m.seq > prev.last_compacted_seq]
        if not unconsolidated:
            break

        trimmed_agent_messages: list[AgentMessage] = []
        for msg in unconsolidated:
            content = msg.processed_context if msg.processed_context else msg.body
            if msg.direction == MessageDirection.INBOUND:
                trimmed_agent_messages.append(UserMessage(content=content))
            else:
                trimmed_agent_messages.append(AssistantMessage(content=content))

        if trimmed_agent_messages:
            max_seq = max(m.seq for m in unconsolidated)
            task = asyncio.create_task(
                _run_compaction_in_background(
                    session_store,
                    prev,
                    user_id,
                    trimmed_agent_messages,
                    max_seq,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            logger.info(
                "Triggered session-end consolidation for user %s: %d messages from session %s",
                user_id,
                len(trimmed_agent_messages),
                sid,
            )
        break


async def get_or_create_conversation(
    user_id: str,
    external_session_id: str | None = None,
    force_new: bool = False,
) -> tuple[SessionState, bool]:
    """Get active conversation or create new one.

    Sessions are persistent: the most recent active session is always reused
    regardless of age.  Pass ``force_new=True`` to explicitly start a fresh
    conversation (e.g. from a "New Conversation" button in the web GUI).
    Returns (session, is_new).

    When a new session is created, any unconsolidated messages from the
    previous session are consolidated in the background.
    """
    session_store = get_session_store(user_id)

    if not force_new and external_session_id is not None:
        session = session_store.load_session(external_session_id)
        if session is not None and session.user_id == user_id:
            return session, False

    session, is_new = await session_store.get_or_create_session(force_new=force_new)

    if is_new and settings.compaction_enabled:
        await _consolidate_previous_session(
            session_store,
            user_id,
            session.session_id,
        )

    return session, is_new
