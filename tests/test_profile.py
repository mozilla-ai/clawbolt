import pytest
from sqlalchemy.orm import Session

from backend.app.agent.profile import (
    build_onboarding_prompt,
    build_soul_prompt,
    update_contractor_profile,
)
from backend.app.models import Contractor


@pytest.mark.asyncio()
async def test_update_contractor_profile(db_session: Session, test_contractor: Contractor) -> None:
    """Should update allowed profile fields."""
    updated = await update_contractor_profile(
        db_session,
        test_contractor,
        {"name": "Mike Chen", "trade": "General Contractor", "hourly_rate": 85.0},
    )
    assert updated.name == "Mike Chen"
    assert updated.trade == "General Contractor"
    assert updated.hourly_rate == 85.0


@pytest.mark.asyncio()
async def test_update_contractor_profile_ignores_unknown_fields(
    db_session: Session, test_contractor: Contractor
) -> None:
    """Should ignore fields not in the allowed set."""
    original_name = test_contractor.name
    await update_contractor_profile(
        db_session, test_contractor, {"id": 999, "unknown_field": "bad"}
    )
    assert test_contractor.name == original_name


def test_build_soul_prompt_full_profile() -> None:
    """Soul prompt should include all profile fields."""
    contractor = Contractor(
        user_id="test",
        name="Mike Chen",
        trade="general contracting",
        location="Portland, OR",
        hourly_rate=85.0,
        business_hours="Mon-Fri 7am-5pm",
        soul_text="I specialize in deck building and exterior renovations.",
    )
    prompt = build_soul_prompt(contractor)
    assert "Mike Chen" in prompt
    assert "general contracting" in prompt
    assert "Portland, OR" in prompt
    assert "$85/hour" in prompt
    assert "Mon-Fri 7am-5pm" in prompt
    assert "deck building" in prompt


def test_build_soul_prompt_minimal_profile() -> None:
    """Soul prompt should work with minimal profile data."""
    contractor = Contractor(user_id="test", name="", trade="", phone="+15551234567")
    prompt = build_soul_prompt(contractor)
    assert "a contractor" in prompt
    assert "contracting" in prompt


def test_build_onboarding_prompt() -> None:
    """Onboarding prompt should include instructions for data collection."""
    prompt = build_onboarding_prompt()
    assert "name" in prompt.lower()
    assert "trade" in prompt.lower()
    assert "rate" in prompt.lower()
