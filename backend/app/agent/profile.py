from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import Contractor


async def get_or_create_contractor(
    db: Session,
    user_id: str,
    phone: str,
) -> tuple[Contractor, bool]:
    """Get existing contractor or create new one. Returns (contractor, is_new)."""
    contractor = db.query(Contractor).filter(Contractor.phone == phone).first()
    if contractor:
        return contractor, False

    contractor = Contractor(user_id=user_id, phone=phone)
    db.add(contractor)
    db.commit()
    db.refresh(contractor)
    return contractor, True


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
    """Build the 'soul' section of the system prompt from contractor profile."""
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

    if contractor.soul_text:
        lines.append(f"\n{contractor.soul_text}")

    return "\n".join(lines)


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
        "Be conversational and warm. Don't ask all questions at once — "
        "let the conversation flow naturally. Start by introducing yourself "
        "and asking their name and what kind of work they do."
    )
