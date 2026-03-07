"""Conversation context loading and session management."""

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from backend.app.agent.compaction import compact_session
from backend.app.agent.file_store import (
    FileSessionStore,
    SessionState,
    get_session_store,
)
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.config import settings
from backend.app.enums import MessageDirection

logger = logging.getLogger(__name__)

CONVERSATION_TIMEOUT_HOURS = settings.conversation_timeout_hours
DEFAULT_HISTORY_LIMIT = settings.conversation_history_limit

# Strong references to fire-and-forget background tasks so they are not
# garbage-collected before completion.
_background_tasks: set[asyncio.Task[None]] = set()


class StoredToolInteraction(BaseModel):
    """Schema for tool interaction records stored in StoredMessage.tool_interactions_json."""

    tool_call_id: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    is_error: bool = False
    tags: set[str] = Field(default_factory=set, exclude=True)


async def _run_compaction_in_background(
    session_store: FileSessionStore,
    session: SessionState,
    contractor_id: int,
    trimmed_agent_messages: list[AgentMessage],
    max_message_seq: int,
) -> None:
    """Run compaction and update the session's tracking field.

    This is designed to be fired as a background task via asyncio.create_task
    so it does not block message processing.
    """
    try:
        saved, compacted_seq = await compact_session(
            contractor_id, trimmed_agent_messages, max_message_seq=max_message_seq
        )
        if compacted_seq is not None:
            await session_store.update_compaction_seq(session, compacted_seq)
        if saved:
            logger.info(
                "Session compaction extracted %d fact(s) from %d trimmed message(s) "
                "for contractor %d",
                len(saved),
                len(trimmed_agent_messages),
                contractor_id,
            )
    except Exception:
        logger.exception(
            "Session compaction failed for session %s, contractor %d",
            session.session_id,
            contractor_id,
        )


def _parse_tool_interactions(raw: str) -> list[StoredToolInteraction]:
    """Parse tool_interactions_json, returning validated models.

    Each item is validated against ``StoredToolInteraction``. Missing fields
    receive defaults (backward compatible). Items that fail validation
    entirely are logged and skipped so corrupt data never crashes loading.
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
    contractor_id: int | None = None,
) -> list[AgentMessage]:
    """Load recent messages as typed message objects for LLM context.

    Returns a list of typed messages in chronological order, excluding the
    most recent (which is the current message being processed).

    For outbound messages that have ``tool_interactions_json``, the full
    tool call/result sequence is reconstructed so the LLM can see its
    prior tool usage.  Old messages without tool interaction data are
    loaded as flat ``AssistantMessage`` (backward compatible).

    When the session has more messages than *limit* and a *contractor_id*
    is provided, the messages that are about to age out are passed through
    session compaction to extract durable facts before they leave the context
    window. Compaction runs as a background task to avoid blocking message
    processing, and only processes messages not already compacted (tracked via
    ``SessionState.last_compacted_seq``).
    """
    all_messages = session.messages
    total_count = len(all_messages)

    # Get the most recent `limit` messages, excluding the current (last) one
    if total_count > 1:
        messages = all_messages[-(limit):][:-1] if total_count > limit else all_messages[:-1]
    else:
        messages = []

    # If messages were trimmed and we have a contractor_id, run compaction
    if contractor_id is not None and total_count > limit:
        trimmed_count = total_count - limit

        # Get the oldest messages that have been trimmed from the context window
        trimmed_msgs = all_messages[:trimmed_count]

        # Filter out messages that have already been compacted
        if session.last_compacted_seq > 0:
            trimmed_msgs = [m for m in trimmed_msgs if m.seq > session.last_compacted_seq]

        trimmed_agent_messages: list[AgentMessage] = []
        for msg in trimmed_msgs:
            content = msg.processed_context if msg.processed_context else msg.body
            if msg.direction == MessageDirection.INBOUND:
                trimmed_agent_messages.append(UserMessage(content=content))
            else:
                trimmed_agent_messages.append(AssistantMessage(content=content))

        if trimmed_agent_messages and trimmed_msgs:
            max_seq = max(m.seq for m in trimmed_msgs)
            session_store = get_session_store(contractor_id)
            task = asyncio.create_task(
                _run_compaction_in_background(
                    session_store,
                    session,
                    contractor_id,
                    trimmed_agent_messages,
                    max_seq,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

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


async def get_or_create_conversation(
    contractor_id: int,
    external_session_id: str | None = None,
    timeout_hours: int = CONVERSATION_TIMEOUT_HOURS,
) -> tuple[SessionState, bool]:
    """Get active conversation or create new one.

    A conversation is "active" if the last message was within the timeout window.
    Returns (session, is_new).
    """
    session_store = get_session_store(contractor_id)

    if external_session_id is not None:
        session = session_store._load_session(external_session_id)
        if session is not None and session.contractor_id == contractor_id:
            return session, False

    return await session_store.get_or_create_session(timeout_hours=timeout_hours)
