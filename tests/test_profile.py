import pytest

from backend.app.agent.file_store import ContractorData
from backend.app.agent.profile import (
    build_onboarding_prompt,
    build_soul_prompt,
    update_contractor_profile,
)


@pytest.mark.asyncio()
async def test_update_contractor_profile(test_contractor: ContractorData) -> None:
    """Should update allowed profile fields."""
    updated = await update_contractor_profile(
        test_contractor,
        {"name": "Mike Chen"},
    )
    assert updated.name == "Mike Chen"


@pytest.mark.asyncio()
async def test_update_contractor_profile_ignores_unknown_fields(
    test_contractor: ContractorData,
) -> None:
    """Should ignore fields not in the allowed set."""
    original_name = test_contractor.name
    await update_contractor_profile(test_contractor, {"id": 999, "unknown_field": "bad"})
    assert test_contractor.name == original_name


def test_build_soul_prompt_full_profile() -> None:
    """Soul prompt should include name and soul_text."""
    contractor = ContractorData(
        user_id="test",
        name="Mike Chen",
        soul_text="I specialize in deck building and exterior renovations.",
    )
    prompt = build_soul_prompt(contractor)
    assert "Clawbolt" in prompt  # default assistant_name
    assert "Mike Chen" in prompt
    assert "deck building" in prompt


def test_build_soul_prompt_uses_assistant_name() -> None:
    """Soul prompt should use custom assistant_name instead of Clawbolt."""
    contractor = ContractorData(
        user_id="test",
        name="Jake",
        assistant_name="Bolt",
    )
    prompt = build_soul_prompt(contractor)
    assert "You are Bolt, the AI assistant for Jake" in prompt
    assert "Clawbolt" not in prompt


def test_build_soul_prompt_minimal_profile() -> None:
    """Soul prompt should work with minimal profile data."""
    contractor = ContractorData(user_id="test", name="", phone="+15551234567")
    prompt = build_soul_prompt(contractor)
    assert "a contractor" in prompt


def test_build_soul_prompt_with_soul_text() -> None:
    """Soul prompt should include custom soul_text."""
    contractor = ContractorData(
        user_id="test",
        name="Jake",
        soul_text="Direct and practical. Keep estimates tight.",
    )
    prompt = build_soul_prompt(contractor)
    assert "Direct and practical" in prompt
    assert "Jake" in prompt


def test_build_onboarding_prompt() -> None:
    """Onboarding prompt should include instructions for data collection."""
    prompt = build_onboarding_prompt()
    assert "name" in prompt.lower()


def test_build_onboarding_prompt_includes_personality_discovery() -> None:
    """Onboarding prompt should include personality/naming discovery."""
    prompt = build_onboarding_prompt()
    assert "assistant_name" in prompt
    assert "SOUL.md" in prompt
    assert "personality" in prompt.lower()


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


class TestSoulPrompt:
    def test_soul_text_included(self) -> None:
        """When soul_text is set, it should appear in the prompt."""
        contractor = ContractorData(
            user_id="test",
            name="Sparky",
            soul_text="I focus on residential panel upgrades only.",
        )
        prompt = build_soul_prompt(contractor)
        assert "residential panel upgrades" in prompt

    def test_no_soul_text(self) -> None:
        """When soul_text is empty, prompt should just have identity."""
        contractor = ContractorData(
            user_id="test",
            name="Bob",
            soul_text="",
        )
        prompt = build_soul_prompt(contractor)
        assert "Bob" in prompt
