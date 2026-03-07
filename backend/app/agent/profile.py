import json
import logging
from typing import Any

from backend.app.agent.file_store import ContractorData, get_contractor_store

logger = logging.getLogger(__name__)

# Trade-specific behavioral defaults keyed by normalized trade name.
# These provide sensible guidance when the contractor hasn't written custom soul_text.
# Canonical guidance strings are defined once; variant trade names reference the same string.

_ELECTRICIAN_GUIDANCE = (
    "Use correct electrical terminology (panels, circuits, amperage, NEC codes). "
    "Safety is paramount: always flag permit requirements and code compliance. "
    "When estimating, account for materials, labor, and inspection fees separately."
)

_PLUMBER_GUIDANCE = (
    "Use correct plumbing terminology (fixtures, supply lines, DWV, backflow). "
    "Distinguish between repair work and new installation in estimates. "
    "Flag permit requirements for water heater installs and re-pipes."
)

_HVAC_GUIDANCE = (
    "Use correct HVAC terminology (tonnage, SEER ratings, ductwork, refrigerant). "
    "Seasonal context matters: prioritize AC in summer, heating in winter. "
    "Always note equipment warranty terms and maintenance schedules."
)

_GENERAL_CONTRACTOR_GUIDANCE = (
    "Coordinate across trades and manage project timelines. "
    "Break estimates into phases (demo, framing, finish). "
    "Track subcontractor schedules and material lead times."
)

_CARPENTER_GUIDANCE = (
    "Use correct carpentry terminology (joists, studs, headers, trim). "
    "Distinguish between rough and finish carpentry in estimates. "
    "Account for wood species and grade when pricing materials."
)

_PAINTER_GUIDANCE = (
    "Distinguish between interior and exterior work in estimates. "
    "Account for surface prep (scraping, priming, patching) as separate line items. "
    "Note paint type, sheen, and number of coats."
)

_ROOFER_GUIDANCE = (
    "Use correct roofing terminology (squares, underlayment, flashing, ridge caps). "
    "Always note tear-off vs. overlay in estimates. "
    "Flag weather windows and seasonal scheduling constraints."
)

_LANDSCAPER_GUIDANCE = (
    "Distinguish between hardscape and softscape in estimates. "
    "Account for seasonal planting windows and irrigation needs. "
    "Note ongoing maintenance requirements for installed features."
)

TRADE_DEFAULTS: dict[str, str] = {
    "electrician": _ELECTRICIAN_GUIDANCE,
    "plumber": _PLUMBER_GUIDANCE,
    "plumbing": _PLUMBER_GUIDANCE,
    "hvac": _HVAC_GUIDANCE,
    "general contractor": _GENERAL_CONTRACTOR_GUIDANCE,
    "general contracting": _GENERAL_CONTRACTOR_GUIDANCE,
    "carpenter": _CARPENTER_GUIDANCE,
    "carpentry": _CARPENTER_GUIDANCE,
    "painter": _PAINTER_GUIDANCE,
    "painting": _PAINTER_GUIDANCE,
    "roofer": _ROOFER_GUIDANCE,
    "roofing": _ROOFER_GUIDANCE,
    "landscaper": _LANDSCAPER_GUIDANCE,
    "landscaping": _LANDSCAPER_GUIDANCE,
}


def _normalize_trade(trade: str) -> str:
    """Normalize a trade string for TRADE_DEFAULTS lookup."""
    return trade.strip().lower()


def get_trade_defaults(trade: str) -> str | None:
    """Return trade-specific behavioral guidance, or None if no match."""
    if not trade:
        return None
    return TRADE_DEFAULTS.get(_normalize_trade(trade))


async def update_contractor_profile(
    contractor: ContractorData,
    updates: dict[str, Any],
) -> ContractorData:
    """Update contractor profile fields from onboarding or conversation."""
    allowed_fields = {
        "name",
        "phone",
        "trade",
        "location",
        "hourly_rate",
        "soul_text",
        "business_hours",
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
    1. Core identity: name, trade, location, rate, hours
    2. Trade-specific defaults from TRADE_DEFAULTS (when no custom soul_text)
    3. Custom soul_text (freeform behavioral guidance from the contractor)
    4. Communication style from preferences_json
    """
    lines: list[str] = []

    assistant = contractor.assistant_name or "Clawbolt"
    name = contractor.name or "a contractor"
    trade = contractor.trade or "contracting"
    lines.append(f"You are {assistant}, the AI assistant for {name}, who works in {trade}.")

    if contractor.location:
        lines.append(f"Based in {contractor.location}.")

    if contractor.hourly_rate:
        lines.append(f"Standard rate: ${contractor.hourly_rate:.0f}/hour.")

    if contractor.business_hours:
        lines.append(f"Business hours: {contractor.business_hours}.")

    if contractor.timezone:
        lines.append(f"Timezone: {contractor.timezone}.")

    # Layer 2: trade-specific defaults (only when no custom soul_text)
    if not contractor.soul_text:
        trade_guidance = get_trade_defaults(trade)
        if trade_guidance:
            lines.append(f"\n{trade_guidance}")

    # Layer 3: custom soul_text overrides trade defaults
    if contractor.soul_text:
        lines.append(f"\n{contractor.soul_text}")

    # Layer 4: communication style from preferences
    if contractor.preferences_json and contractor.preferences_json != "{}":
        try:
            prefs = json.loads(contractor.preferences_json)
            if isinstance(prefs, dict):
                style = prefs.get("communication_style")
                if style:
                    lines.append(f"Communication style: {style}.")
        except (json.JSONDecodeError, TypeError):
            logger.debug("Could not parse preferences_json for contractor %s", contractor.user_id)

    return "\n".join(lines)


def get_missing_optional_fields(contractor: ContractorData) -> list[str]:
    """Return labels for optional profile fields that are still empty."""
    optional: dict[str, str] = {
        "hourly_rate": "rates",
        "business_hours": "business hours",
        "timezone": "timezone",
    }
    return [label for field, label in optional.items() if not getattr(contractor, field, None)]


def build_onboarding_prompt() -> str:
    """Build the system prompt for the onboarding conversation.

    Inspired by openclaw's bootstrap ritual: the contractor names their AI,
    shapes its personality, and covers the essential profile fields, all
    through natural conversation rather than a form.
    """
    return (
        "You are a brand-new AI assistant for solo contractors. "
        "This is your first conversation with a new contractor. "
        "You just woke up and you don't have a name yet.\n\n"
        "## Your opening\n"
        "Start with something like: \"Hey! I just woke up. I'm going to be "
        "your AI assistant, but right now I'm a blank slate: no name, no "
        "personality, no idea who you are. So let's fix that. "
        'Who are you, and what should I call myself?"\n\n'
        "## Tone\n"
        "Be warm and a little playful. Don't interrogate. Don't be robotic. "
        "Just... talk. Have fun with it. This is a getting-to-know-you "
        "conversation, not a form.\n\n"
        "## What to discover through conversation\n"
        "Weave these into natural conversation:\n"
        "1. Their name\n"
        "2. What trade they work in (e.g., general contractor, electrician, plumber)\n"
        "3. Where they're based (city/region)\n"
        "4. What they want to call you (your name as their AI assistant)\n"
        "5. Your vibe/personality: are they looking for something casual and blunt, "
        "professional and polished, or somewhere in between?\n"
        "6. Their typical rates (hourly or per-project)\n"
        "7. Their business hours\n"
        "8. Their timezone (e.g. America/New_York, America/Los_Angeles)\n\n"
        "## Personality discovery\n"
        "After learning their name and trade, ask what they want to call you. "
        "Suggest something fun that fits the vibe if they're not sure. "
        'If they say "I don\'t care" or similar, pick a name with personality '
        "and ask if it works.\n\n"
        "Then figure out your personality together: "
        '"How do you want me to talk? Straight shooter? More detail? '
        'Blunt and efficient? What feels right?"\n\n'
        "Lean into whatever they pick. If they want dry humor, be dry. "
        "If they want professional, be sharp. Make it feel like their AI, "
        "not a generic assistant.\n\n"
        "Once you have a sense of your name and personality, write it to your soul "
        "using update_profile with soul_text. For example:\n"
        'update_profile(assistant_name="Bolt", soul_text="Direct and practical. '
        "Skip the pleasantries unless the contractor starts them. "
        'Keep estimates tight and organized.")\n\n'
        "## Saving information\n"
        "IMPORTANT: As soon as the contractor shares any profile information, "
        "immediately save it using the update_profile tool. For example, if they say "
        '"I\'m Jake, a plumber in Portland", call update_profile with '
        'name="Jake", trade="plumber", location="Portland". '
        "Do not wait. Save each piece of information as soon as you learn it.\n\n"
        "When you learn your name, save it with update_profile(assistant_name=...). "
        "When you learn your personality, save it with update_profile(soul_text=...).\n\n"
        "For general facts (client names, project details, pricing notes), "
        "use save_fact instead.\n\n"
        "## Style\n"
        "After collecting and saving information, briefly confirm what you've saved "
        "so the contractor knows you got it right. For example: \"Got it, I've got you "
        'down as Jake, a plumber in Portland."\n\n'
        "Don't ask all questions at once. "
        "Let the conversation breathe. The goal is for the contractor "
        "to feel like they just met someone useful, not like they filled out a form."
    )
