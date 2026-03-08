import logging
from typing import Any

from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.agent.prompts import load_prompt

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
        "user_text",
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
    1. Core identity: name, trade, location
    2. Trade-specific defaults from TRADE_DEFAULTS (when no custom soul_text)
    3. Custom soul_text (freeform behavioral guidance from the contractor)
    """
    lines: list[str] = []

    assistant = contractor.assistant_name or "Clawbolt"
    name = contractor.name or "a contractor"
    trade = contractor.trade or "contracting"
    lines.append(f"You are {assistant}, the AI assistant for {name}, who works in {trade}.")

    if contractor.location:
        lines.append(f"Based in {contractor.location}.")

    # Layer 2: trade-specific defaults (only when no custom soul_text)
    if not contractor.soul_text:
        trade_guidance = get_trade_defaults(trade)
        if trade_guidance:
            lines.append(f"\n{trade_guidance}")

    # Layer 3: custom soul_text overrides trade defaults
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
