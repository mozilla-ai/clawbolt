"""Onboarding conversation logic for new contractors."""

import logging
import re
from typing import Any

from backend.app.agent.core import AgentResponse
from backend.app.agent.profile import build_onboarding_prompt
from backend.app.models import Contractor

logger = logging.getLogger(__name__)

# Fields that indicate a contractor has completed onboarding
REQUIRED_PROFILE_FIELDS = {"name", "trade"}


def is_onboarding_needed(contractor: Contractor) -> bool:
    """Check if contractor needs onboarding.

    Returns False once onboarding_complete is set, or if all required
    profile fields are already populated.
    """
    if contractor.onboarding_complete:
        return False
    for field in REQUIRED_PROFILE_FIELDS:
        value = getattr(contractor, field, None)
        if not value or not str(value).strip():
            return True
    return False


def build_onboarding_system_prompt(contractor: Contractor) -> str:
    """Build system prompt for onboarding mode.

    Wraps the base onboarding prompt with any partial profile info
    already collected so the agent doesn't re-ask known fields.
    """
    base = build_onboarding_prompt()

    known: list[str] = []
    if contractor.name and contractor.name.strip():
        known.append(f"- Name: {contractor.name}")
    if contractor.trade and contractor.trade.strip():
        known.append(f"- Trade: {contractor.trade}")
    if contractor.location and contractor.location.strip():
        known.append(f"- Location: {contractor.location}")
    if contractor.hourly_rate:
        known.append(f"- Rate: ${contractor.hourly_rate:.0f}/hour")
    if contractor.business_hours and contractor.business_hours.strip():
        known.append(f"- Business hours: {contractor.business_hours}")

    parts = [base]
    if known:
        parts.append("\n\nYou already know:\n" + "\n".join(known) + "\n\nDon't re-ask these.")

    parts.append(
        "\n\nIMPORTANT: If the contractor asks about something specific (a quote, a question, "
        "a photo), help them with that request FIRST, then naturally weave in any remaining "
        "onboarding questions. Never ignore their request just to collect profile info."
    )

    return "".join(parts)


def _parse_rate(value: str) -> float | None:
    """Extract a numeric rate from natural-language rate descriptions.

    Handles formats like "$85/hr", "$85/hour", "$85 per hour", "$85 an hour",
    "85 dollars", "$85.50", "$50-75/hr" (extracts first number), "$4500 per project",
    "Usually around $80", etc.

    Returns None for non-numeric values like "not sure" or "varies".
    """
    cleaned = str(value).replace(",", "").strip()

    # Try to find a dollar amount or plain number
    # Handles: $85, $85/hr, $85.50, 85, 85.00, etc.
    match = re.search(r"\$?\s*(\d+(?:\.\d+)?)", cleaned)
    if match:
        return float(match.group(1))

    return None


def _match_profile_field(key: str) -> str | None:
    """Match a fact key to a contractor profile field using keyword matching.

    Handles common synonyms and variations that an LLM might use instead
    of the exact expected key names (e.g. "profession" instead of "trade").
    Returns the canonical profile field name, or None if no match.
    """
    key_lower = key.lower().strip().replace("_", " ").replace("-", " ")
    tokens = key_lower.split()

    # "name" matching - require "name" as a standalone token to avoid false
    # positives on words like "username" or "filename"
    if "name" in tokens:
        return "name"

    if any(
        w in key_lower for w in ["trade", "profession", "specialty", "craft", "occupation", "job"]
    ):
        return "trade"
    if any(
        w in key_lower for w in ["location", "city", "region", "area", "based", "address", "town"]
    ):
        return "location"
    if any(w in key_lower for w in ["rate", "price", "pricing", "hourly", "charge", "cost"]):
        return "hourly_rate"
    if any(
        w in key_lower
        for w in ["hours", "schedule", "availability", "work hours", "business hours"]
    ):
        return "business_hours"
    return None


def extract_profile_updates(agent_response: AgentResponse) -> dict[str, Any]:
    """Extract profile field updates from agent tool calls during onboarding.

    Looks at save_fact calls and maps known categories to profile fields.
    Uses exact key lookup as the fast path, then falls back to fuzzy keyword
    matching for synonym keys the LLM might use.
    """
    updates: dict[str, Any] = {}

    # Map memory keys to profile fields (exact fast path)
    key_to_field: dict[str, str] = {
        "name": "name",
        "contractor_name": "name",
        "trade": "trade",
        "location": "location",
        "city": "location",
        "region": "location",
        "hourly_rate": "hourly_rate",
        "rate": "hourly_rate",
        "business_hours": "business_hours",
        "hours": "business_hours",
    }

    for tc in agent_response.tool_calls:
        if tc.get("name") != "save_fact":
            continue
        args = tc.get("args", {})
        key = str(args.get("key", "")).lower().strip()
        value = args.get("value", "")

        # Try exact lookup first, then fall back to fuzzy matching
        field = key_to_field.get(key) or _match_profile_field(key)

        if field is not None:
            if field == "hourly_rate":
                parsed = _parse_rate(str(value))
                if parsed is not None:
                    updates[field] = parsed
                else:
                    logger.warning("Could not parse hourly rate from value: %r", value)
            else:
                updates[field] = str(value)

    return updates
