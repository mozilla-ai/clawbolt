import logging
from typing import Any

from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.agent.prompts import load_prompt

logger = logging.getLogger(__name__)


async def update_contractor_profile(
    contractor: ContractorData,
    updates: dict[str, Any],
) -> ContractorData:
    """Update contractor profile fields from onboarding or conversation."""
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
        return contractor
    store = get_contractor_store()
    updated = await store.update(contractor.id, **filtered)
    return updated or contractor


def build_soul_prompt(contractor: ContractorData) -> str:
    """Build the 'soul' section of the system prompt from contractor profile.

    Layers (in order):
    1. Core identity: name and assistant name
    2. Custom soul_text (freeform behavioral guidance from the contractor)
    """
    lines: list[str] = []

    assistant = contractor.assistant_name or "Clawbolt"
    name = contractor.name or "a contractor"
    lines.append(f"You are {assistant}, the AI assistant for {name}.")

    # Custom soul_text for personality and behavioral guidance
    if contractor.soul_text:
        lines.append(f"\n{contractor.soul_text}")

    return "\n".join(lines)


def build_onboarding_prompt() -> str:
    """Build the system prompt for the onboarding conversation.

    Inspired by openclaw's bootstrap ritual: the contractor names their AI,
    shapes its personality, and covers the essential profile fields, all
    through natural conversation rather than a form.
    """
    return load_prompt("onboarding")
