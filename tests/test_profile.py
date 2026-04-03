from backend.app.agent.system_prompt import build_soul_prompt
from backend.app.models import User


def test_build_soul_prompt_with_soul_text() -> None:
    """Soul prompt should return soul_text content."""
    user = User(
        user_id="test",
        soul_text="I'm Bolt. Direct and practical. Keep estimates tight.",
    )
    prompt = build_soul_prompt(user)
    assert "Direct and practical" in prompt


def test_build_soul_prompt_empty_soul_text() -> None:
    """Soul prompt should return empty string when no soul_text."""
    user = User(user_id="test", soul_text="")
    prompt = build_soul_prompt(user)
    assert prompt == ""


def test_build_soul_prompt_with_identity() -> None:
    """Soul prompt should include identity info written to SOUL.md."""
    user = User(
        user_id="test",
        soul_text="I'm Bolt, the AI assistant for Jake. Direct and practical.",
    )
    prompt = build_soul_prompt(user)
    assert "Bolt" in prompt
    assert "Jake" in prompt


class TestSoulPrompt:
    def test_soul_text_included(self) -> None:
        """When soul_text is set, it should appear in the prompt."""
        user = User(
            user_id="test",
            soul_text="I focus on residential panel upgrades only.",
        )
        prompt = build_soul_prompt(user)
        assert "residential panel upgrades" in prompt

    def test_no_soul_text(self) -> None:
        """When soul_text is empty, prompt should be empty string."""
        user = User(
            user_id="test",
            soul_text="",
        )
        prompt = build_soul_prompt(user)
        assert prompt == ""
