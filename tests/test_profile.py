import json

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.profile import (
    TRADE_DEFAULTS,
    build_onboarding_prompt,
    build_soul_prompt,
    get_trade_defaults,
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


def test_build_soul_prompt_includes_preferences_json() -> None:
    """Soul prompt should render communication style from preferences_json."""
    contractor = Contractor(
        user_id="test",
        name="Jake",
        trade="plumbing",
        preferences_json=json.dumps({"communication_style": "casual and brief"}),
    )
    prompt = build_soul_prompt(contractor)
    assert "Communication style: casual and brief." in prompt


def test_build_soul_prompt_ignores_empty_preferences() -> None:
    """Soul prompt should not include communication style when preferences_json is empty."""
    contractor = Contractor(
        user_id="test",
        name="Jake",
        trade="plumbing",
        preferences_json="{}",
    )
    prompt = build_soul_prompt(contractor)
    assert "Communication style" not in prompt


def test_build_soul_prompt_handles_malformed_preferences() -> None:
    """Soul prompt should gracefully handle malformed preferences_json."""
    contractor = Contractor(
        user_id="test",
        name="Jake",
        trade="plumbing",
        preferences_json="not valid json",
    )
    prompt = build_soul_prompt(contractor)
    # Should not raise, and should not include communication style
    assert "Communication style" not in prompt
    assert "Jake" in prompt


def test_build_onboarding_prompt() -> None:
    """Onboarding prompt should include instructions for data collection."""
    prompt = build_onboarding_prompt()
    assert "name" in prompt.lower()
    assert "trade" in prompt.lower()
    assert "rate" in prompt.lower()


def test_build_onboarding_prompt_includes_confirmation_instruction() -> None:
    """Onboarding prompt should instruct agent to confirm saved info."""
    prompt = build_onboarding_prompt()
    assert "confirm what you've saved" in prompt


def test_build_onboarding_prompt_mentions_update_profile_tool() -> None:
    """Onboarding prompt should mention update_profile as the tool for profile data."""
    prompt = build_onboarding_prompt()
    assert "update_profile" in prompt


def test_build_onboarding_prompt_mentions_save_fact_for_general() -> None:
    """Onboarding prompt should mention save_fact for general facts."""
    prompt = build_onboarding_prompt()
    assert "save_fact" in prompt


# -----------------------------------------------------------------------
# TRADE_DEFAULTS and trade-specific personality tests
# -----------------------------------------------------------------------


class TestTradeDefaults:
    def test_trade_defaults_has_common_trades(self) -> None:
        """TRADE_DEFAULTS should include entries for common trades."""
        expected = ["electrician", "plumber", "hvac", "general contractor", "carpenter"]
        for trade in expected:
            assert trade in TRADE_DEFAULTS, f"Missing trade: {trade}"

    def test_get_trade_defaults_exact_match(self) -> None:
        """Should return guidance for an exact trade name match."""
        result = get_trade_defaults("electrician")
        assert result is not None
        assert "electrical" in result.lower()

    def test_get_trade_defaults_case_insensitive(self) -> None:
        """Should match trades case-insensitively."""
        result = get_trade_defaults("Electrician")
        assert result is not None
        assert "electrical" in result.lower()

    def test_get_trade_defaults_strips_whitespace(self) -> None:
        """Should strip leading/trailing whitespace before matching."""
        result = get_trade_defaults("  plumber  ")
        assert result is not None
        assert "plumbing" in result.lower()

    def test_get_trade_defaults_unknown_trade(self) -> None:
        """Should return None for an unrecognized trade."""
        result = get_trade_defaults("underwater basket weaving")
        assert result is None

    def test_get_trade_defaults_empty_string(self) -> None:
        """Should return None for empty string."""
        result = get_trade_defaults("")
        assert result is None

    def test_trade_defaults_no_em_dashes(self) -> None:
        """Trade defaults must not contain em dashes per coding standards."""
        for trade, guidance in TRADE_DEFAULTS.items():
            assert "\u2014" not in guidance, f"Em dash found in trade defaults for {trade}"
            assert "\u2013" not in guidance, f"En dash found in trade defaults for {trade}"


class TestSoulPromptWithTradeDefaults:
    def test_trade_defaults_included_without_soul_text(self) -> None:
        """When soul_text is empty, trade defaults should appear in the prompt."""
        contractor = Contractor(
            user_id="test",
            name="Sparky",
            trade="electrician",
            soul_text="",
        )
        prompt = build_soul_prompt(contractor)
        assert "electrical" in prompt.lower()
        assert "NEC codes" in prompt

    def test_soul_text_overrides_trade_defaults(self) -> None:
        """When soul_text is set, trade defaults should NOT appear."""
        contractor = Contractor(
            user_id="test",
            name="Sparky",
            trade="electrician",
            soul_text="I focus on residential panel upgrades only.",
        )
        prompt = build_soul_prompt(contractor)
        assert "residential panel upgrades" in prompt
        # Trade defaults should be absent because soul_text is set
        assert "NEC codes" not in prompt

    def test_no_trade_defaults_for_unknown_trade(self) -> None:
        """Unknown trades should produce a prompt without trade guidance."""
        contractor = Contractor(
            user_id="test",
            name="Bob",
            trade="chimney sweep",
            soul_text="",
        )
        prompt = build_soul_prompt(contractor)
        assert "chimney sweep" in prompt
        # No trade defaults, so the prompt should be short (identity only)
        assert "Trade guidance" not in prompt

    def test_trade_defaults_with_variant_names(self) -> None:
        """Both 'plumber' and 'plumbing' should produce the same guidance."""
        contractor_a = Contractor(user_id="a", name="A", trade="plumber", soul_text="")
        contractor_b = Contractor(user_id="b", name="B", trade="plumbing", soul_text="")
        prompt_a = build_soul_prompt(contractor_a)
        prompt_b = build_soul_prompt(contractor_b)
        # Both should contain plumbing terminology guidance
        assert "plumbing terminology" in prompt_a.lower()
        assert "plumbing terminology" in prompt_b.lower()

    def test_soul_text_and_preferences_coexist(self) -> None:
        """Soul text and communication style should both appear."""
        contractor = Contractor(
            user_id="test",
            name="Jake",
            trade="plumbing",
            soul_text="I prefer detailed breakdowns for every estimate.",
            preferences_json=json.dumps({"communication_style": "casual"}),
        )
        prompt = build_soul_prompt(contractor)
        assert "detailed breakdowns" in prompt
        assert "Communication style: casual." in prompt

    def test_trade_defaults_and_preferences_coexist(self) -> None:
        """Trade defaults and communication style should both appear when no soul_text."""
        contractor = Contractor(
            user_id="test",
            name="Jake",
            trade="plumbing",
            soul_text="",
            preferences_json=json.dumps({"communication_style": "brief"}),
        )
        prompt = build_soul_prompt(contractor)
        assert "plumbing terminology" in prompt.lower()
        assert "Communication style: brief." in prompt
