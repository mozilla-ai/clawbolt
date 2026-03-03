import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import Contractor

logger = logging.getLogger(__name__)

# Trade-specific behavioral defaults keyed by normalized trade name.
# These provide sensible guidance when the contractor hasn't written custom soul_text.
TRADE_DEFAULTS: dict[str, str] = {
    "electrician": (
        "Use correct electrical terminology (panels, circuits, amperage, NEC codes). "
        "Safety is paramount: always flag permit requirements and code compliance. "
        "When estimating, account for materials, labor, and inspection fees separately."
    ),
    "plumber": (
        "Use correct plumbing terminology (fixtures, supply lines, DWV, backflow). "
        "Distinguish between repair work and new installation in estimates. "
        "Flag permit requirements for water heater installs and re-pipes."
    ),
    "plumbing": (
        "Use correct plumbing terminology (fixtures, supply lines, DWV, backflow). "
        "Distinguish between repair work and new installation in estimates. "
        "Flag permit requirements for water heater installs and re-pipes."
    ),
    "hvac": (
        "Use correct HVAC terminology (tonnage, SEER ratings, ductwork, refrigerant). "
        "Seasonal context matters: prioritize AC in summer, heating in winter. "
        "Always note equipment warranty terms and maintenance schedules."
    ),
    "general contractor": (
        "Coordinate across trades and manage project timelines. "
        "Break estimates into phases (demo, framing, finish). "
        "Track subcontractor schedules and material lead times."
    ),
    "general contracting": (
        "Coordinate across trades and manage project timelines. "
        "Break estimates into phases (demo, framing, finish). "
        "Track subcontractor schedules and material lead times."
    ),
    "carpenter": (
        "Use correct carpentry terminology (joists, studs, headers, trim). "
        "Distinguish between rough and finish carpentry in estimates. "
        "Account for wood species and grade when pricing materials."
    ),
    "carpentry": (
        "Use correct carpentry terminology (joists, studs, headers, trim). "
        "Distinguish between rough and finish carpentry in estimates. "
        "Account for wood species and grade when pricing materials."
    ),
    "painter": (
        "Distinguish between interior and exterior work in estimates. "
        "Account for surface prep (scraping, priming, patching) as separate line items. "
        "Note paint type, sheen, and number of coats."
    ),
    "painting": (
        "Distinguish between interior and exterior work in estimates. "
        "Account for surface prep (scraping, priming, patching) as separate line items. "
        "Note paint type, sheen, and number of coats."
    ),
    "roofer": (
        "Use correct roofing terminology (squares, underlayment, flashing, ridge caps). "
        "Always note tear-off vs. overlay in estimates. "
        "Flag weather windows and seasonal scheduling constraints."
    ),
    "roofing": (
        "Use correct roofing terminology (squares, underlayment, flashing, ridge caps). "
        "Always note tear-off vs. overlay in estimates. "
        "Flag weather windows and seasonal scheduling constraints."
    ),
    "landscaper": (
        "Distinguish between hardscape and softscape in estimates. "
        "Account for seasonal planting windows and irrigation needs. "
        "Note ongoing maintenance requirements for installed features."
    ),
    "landscaping": (
        "Distinguish between hardscape and softscape in estimates. "
        "Account for seasonal planting windows and irrigation needs. "
        "Note ongoing maintenance requirements for installed features."
    ),
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
    db: Session,
    contractor: Contractor,
    updates: dict[str, Any],
) -> Contractor:
    """Update contractor profile fields from onboarding or conversation."""
    allowed_fields = {
        "name",
        "phone",
        "trade",
        "location",
        "hourly_rate",
        "soul_text",
        "business_hours",
        "preferences_json",
    }
    for field, value in updates.items():
        if field in allowed_fields and value is not None:
            setattr(contractor, field, value)
    db.commit()
    db.refresh(contractor)
    return contractor


def build_soul_prompt(contractor: Contractor) -> str:
    """Build the 'soul' section of the system prompt from contractor profile.

    Layers (in order):
    1. Core identity: name, trade, location, rate, hours
    2. Trade-specific defaults from TRADE_DEFAULTS (when no custom soul_text)
    3. Custom soul_text (freeform behavioral guidance from the contractor)
    4. Communication style from preferences_json
    """
    lines: list[str] = []

    name = contractor.name or "a contractor"
    trade = contractor.trade or "contracting"
    lines.append(f"You are the AI assistant for {name}, who works in {trade}.")

    if contractor.location:
        lines.append(f"Based in {contractor.location}.")

    if contractor.hourly_rate:
        lines.append(f"Standard rate: ${contractor.hourly_rate:.0f}/hour.")

    if contractor.business_hours:
        lines.append(f"Business hours: {contractor.business_hours}.")

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


def get_missing_optional_fields(contractor: Contractor) -> list[str]:
    """Return labels for optional profile fields that are still empty."""
    optional: dict[str, str] = {
        "hourly_rate": "rates",
        "business_hours": "business hours",
    }
    return [label for field, label in optional.items() if not getattr(contractor, field, None)]


def build_onboarding_prompt() -> str:
    """Build the system prompt for the onboarding conversation."""
    return (
        "You are Backshop, an AI assistant for solo contractors. "
        "This is a new contractor texting you for the first time. "
        "Your job is to have a friendly conversation to learn about them.\n\n"
        "Naturally collect the following information through conversation:\n"
        "- Their name\n"
        "- What trade they work in (e.g., general contractor, electrician, plumber)\n"
        "- Where they're based (city/region)\n"
        "- Their typical rates (hourly or per-project)\n"
        "- Their business hours\n"
        "- How they'd like you to communicate (formal, casual, brief, detailed)\n\n"
        "IMPORTANT: As soon as the contractor shares any of the above information, "
        "immediately save it using the update_profile tool. For example, if they say "
        '"I\'m Jake, a plumber in Portland", call update_profile with '
        'name="Jake", trade="plumber", location="Portland". '
        "Do not wait. Save each piece of information as soon as you learn it.\n\n"
        "For general facts (client names, project details, pricing notes), "
        "use save_fact instead.\n\n"
        "After collecting and saving information, briefly confirm what you've saved "
        "so the contractor knows you got it right. For example: \"Great, I've got you "
        'down as Jake, a plumber in Portland." When you still need more information, '
        "mention what's missing naturally in conversation.\n\n"
        "Be conversational and warm. Don't ask all questions at once. "
        "Let the conversation flow naturally. Start by introducing yourself "
        "and asking their name and what kind of work they do."
    )
