from backend.app.agent.file_store import UserData
from backend.app.agent.profile import (
    build_onboarding_prompt,
    build_soul_prompt,
)


def test_build_soul_prompt_with_soul_text() -> None:
    """Soul prompt should return soul_text content."""
    user = UserData(
        user_id="test",
        soul_text="I'm Bolt. Direct and practical. Keep estimates tight.",
    )
    prompt = build_soul_prompt(user)
    assert "Direct and practical" in prompt


def test_build_soul_prompt_empty_soul_text() -> None:
    """Soul prompt should return empty string when no soul_text."""
    user = UserData(user_id="test", soul_text="")
    prompt = build_soul_prompt(user)
    assert prompt == ""


def test_build_soul_prompt_with_identity() -> None:
    """Soul prompt should include identity info written to SOUL.md."""
    user = UserData(
        user_id="test",
        soul_text="I'm Bolt, the AI assistant for Jake. Direct and practical.",
    )
    prompt = build_soul_prompt(user)
    assert "Bolt" in prompt
    assert "Jake" in prompt


def test_build_onboarding_prompt() -> None:
    """Onboarding prompt should include instructions for data collection."""
    prompt = build_onboarding_prompt()
    assert "name" in prompt.lower()


def test_build_onboarding_prompt_includes_personality_discovery() -> None:
    """Onboarding prompt should include personality/naming discovery."""
    prompt = build_onboarding_prompt()
    assert "SOUL.md" in prompt
    assert "personality" in prompt.lower()


def test_build_onboarding_prompt_includes_confirmation_instruction() -> None:
    """Onboarding prompt should instruct agent to confirm saved info."""
    prompt = build_onboarding_prompt()
    assert "confirm what you've saved" in prompt


def test_build_onboarding_prompt_mentions_write_file() -> None:
    """Onboarding prompt should mention write_file for saving profile data."""
    prompt = build_onboarding_prompt()
    assert "write_file" in prompt


def test_build_onboarding_prompt_mentions_save_fact_for_general() -> None:
    """Onboarding prompt should mention save_fact for general facts."""
    prompt = build_onboarding_prompt()
    assert "save_fact" in prompt


def test_build_onboarding_prompt_mentions_delete_file() -> None:
    """Onboarding prompt should mention delete_file for completion."""
    prompt = build_onboarding_prompt()
    assert "delete_file" in prompt


class TestSoulPrompt:
    def test_soul_text_included(self) -> None:
        """When soul_text is set, it should appear in the prompt."""
        user = UserData(
            user_id="test",
            soul_text="I focus on residential panel upgrades only.",
        )
        prompt = build_soul_prompt(user)
        assert "residential panel upgrades" in prompt

    def test_no_soul_text(self) -> None:
        """When soul_text is empty, prompt should be empty string."""
        user = UserData(
            user_id="test",
            soul_text="",
        )
        prompt = build_soul_prompt(user)
        assert prompt == ""
