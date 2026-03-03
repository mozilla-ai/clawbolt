"""Conversation context loading and session management."""

import datetime
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.models import Conversation, Message

logger = logging.getLogger(__name__)

CONVERSATION_TIMEOUT_HOURS = settings.conversation_timeout_hours
DEFAULT_HISTORY_LIMIT = settings.conversation_history_limit


def _parse_tool_interactions(raw: str) -> list[dict[str, Any]]:
    """Parse tool_interactions_json, returning an empty list on failure."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        logger.debug("Could not parse tool_interactions_json, falling back to flat text")
    return []


def _expand_outbound_with_tools(
    tool_interactions: list[dict[str, Any]],
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
                id=tc.get("tool_call_id", ""),
                name=tc.get("name", ""),
                arguments=tc.get("args", {}),
            )
        )

    # AssistantMessage requesting the tool calls (content is typically None)
    messages.append(AssistantMessage(content=None, tool_calls=tool_call_requests))

    # ToolResultMessages for each tool execution
    for tc in tool_interactions:
        messages.append(
            ToolResultMessage(
                tool_call_id=tc.get("tool_call_id", ""),
                content=tc.get("result", ""),
            )
        )

    # Final AssistantMessage with the reply text
    messages.append(AssistantMessage(content=reply_text))

    return messages


async def load_conversation_history(
    db: Session,
    conversation_id: int,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[AgentMessage]:
    """Load recent messages as typed message objects for LLM context.

    Returns a list of typed messages in chronological order, excluding the
    most recent (which is the current message being processed).

    For outbound messages that have ``tool_interactions_json``, the full
    tool call/result sequence is reconstructed so the LLM can see its
    prior tool usage.  Old messages without tool interaction data are
    loaded as flat ``AssistantMessage`` (backward compatible).
    """
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    # Reverse to chronological order, skip the current (most recent) message
    messages = list(reversed(messages))[:-1] if len(messages) > 1 else []

    history: list[AgentMessage] = []
    for msg in messages:
        # Prefer processed context (includes media descriptions) over raw body
        content = msg.processed_context if msg.processed_context else msg.body
        if msg.direction == MessageDirection.INBOUND:
            history.append(UserMessage(content=content))
        else:
            # Check for stored tool interactions
            tool_interactions = _parse_tool_interactions(getattr(msg, "tool_interactions_json", ""))
            if tool_interactions:
                history.extend(_expand_outbound_with_tools(tool_interactions, content))
            else:
                history.append(AssistantMessage(content=content))
    return history


async def get_or_create_conversation(
    db: Session,
    contractor_id: int,
    external_session_id: str | None = None,
    timeout_hours: int = CONVERSATION_TIMEOUT_HOURS,
) -> tuple[Conversation, bool]:
    """Get active conversation or create new one.

    A conversation is "active" if the last message was within the timeout window.
    Returns (conversation, is_new).
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=timeout_hours)

    # Look for an active conversation within the timeout window
    active = (
        db.query(Conversation)
        .filter(
            Conversation.contractor_id == contractor_id,
            Conversation.is_active.is_(True),
            Conversation.last_message_at >= cutoff,
        )
        .order_by(Conversation.last_message_at.desc())
        .first()
    )

    if active:
        # Update last_message_at timestamp
        active.last_message_at = datetime.datetime.now(datetime.UTC)
        db.commit()
        db.refresh(active)
        return active, False

    # Create a new conversation
    conversation = Conversation(
        contractor_id=contractor_id,
        external_session_id=external_session_id or "",
        is_active=True,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation, True
