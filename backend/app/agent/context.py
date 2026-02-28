"""Conversation context loading and session management."""

import datetime
import logging

from sqlalchemy.orm import Session

from backend.app.models import Conversation, Message

logger = logging.getLogger(__name__)

CONVERSATION_TIMEOUT_HOURS = 4
DEFAULT_HISTORY_LIMIT = 20


async def load_conversation_history(
    db: Session,
    conversation_id: int,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[dict[str, str]]:
    """Load recent messages formatted for LLM context.

    Returns list of {"role": "user"/"assistant", "content": "..."} dicts.
    Messages are returned in chronological order, excluding the most recent
    (which is the current message being processed).
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

    history: list[dict[str, str]] = []
    for msg in messages:
        role = "user" if msg.direction == "inbound" else "assistant"
        # Prefer processed context (includes media descriptions) over raw body
        content = msg.processed_context if msg.processed_context else msg.body
        history.append({"role": role, "content": content})
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
