import logging
from typing import Any

from backend.app.agent.file_store import UserData, get_user_store
from backend.app.agent.prompts import load_prompt

logger = logging.getLogger(__name__)


async def update_user_profile(
    user: UserData,
    updates: dict[str, Any],
) -> UserData:
    """Update user profile fields from onboarding or conversation."""
    allowed_fields = {
        "name",
        "phone",
        "soul_text",
        "user_text",
        "timezone",
        "preferences_json",
        "assistant_name",
    }
    filtered = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}
    if not filtered:
        return user
    store = get_user_store()
    updated = await store.update(user.id, **filtered)
    return updated or user


def build_soul_prompt(user: UserData) -> str:
    """Build the 'soul' section of the system prompt from user profile.

    Layers (in order):
    1. Core identity: name and assistant name
    2. Custom soul_text (freeform behavioral guidance from the user)
    """
    lines: list[str] = []

    assistant = user.assistant_name or "Clawbolt"
    name = user.name or "a user"
    lines.append(f"You are {assistant}, the AI assistant for {name}.")

    # Custom soul_text for personality and behavioral guidance
    if user.soul_text:
        lines.append(f"\n{user.soul_text}")

    return "\n".join(lines)


def build_onboarding_prompt() -> str:
    """Build the system prompt for the onboarding conversation.

    Inspired by openclaw's bootstrap ritual: the user names their AI,
    shapes its personality, and covers the essential profile fields, all
    through natural conversation rather than a form.
    """
    return load_prompt("onboarding")
