"""Onboarding conversation logic for new contractors."""

import json
import logging

from backend.app.agent.profile import build_onboarding_prompt
from backend.app.models import Contractor

logger = logging.getLogger(__name__)

# Fields that indicate a contractor has completed onboarding
REQUIRED_PROFILE_FIELDS = {"name", "trade", "location"}


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
    if contractor.preferences_json and contractor.preferences_json != "{}":
        try:
            prefs = json.loads(contractor.preferences_json)
            if isinstance(prefs, dict):
                style = prefs.get("communication_style")
                if style:
                    known.append(f"- Communication style: {style}")
        except (json.JSONDecodeError, TypeError):
            pass

    parts = [base]
    if known:
        parts.append("\n\nYou already know:\n" + "\n".join(known) + "\n\nDon't re-ask these.")

    parts.append(
        "\n\nIMPORTANT: If the contractor asks about something specific (a quote, a question, "
        "a photo), help them with that request FIRST, then naturally weave in any remaining "
        "onboarding questions. Never ignore their request just to collect profile info."
    )

    return "".join(parts)
