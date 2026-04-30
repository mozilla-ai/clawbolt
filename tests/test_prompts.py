"""Tests for the prompt loader utility."""

import pytest

from backend.app.agent.prompts import load_prompt

ALL_PROMPT_NAMES = [
    "bootstrap",
    "compaction",
    "instructions",
    "proactive",
    "heartbeat_preamble",
    "heartbeat_rules",
    "default_soul",
]


@pytest.mark.parametrize("name", ALL_PROMPT_NAMES)
def test_load_prompt_returns_string(name: str) -> None:
    result = load_prompt(name)
    assert isinstance(result, str)
    assert len(result) > 0


def test_load_prompt_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_that_does_not_exist")


def test_load_prompt_strips_whitespace() -> None:
    result = load_prompt("instructions")
    assert not result.startswith("\n")
    assert not result.endswith("\n")


def test_load_prompt_content_sanity() -> None:
    """Verify key substrings are present in a few prompts."""
    assert "concise" in load_prompt("instructions")
    assert "JSON" in load_prompt("compaction")
    assert "tradesperson" in load_prompt("bootstrap")


def test_bootstrap_identifies_as_clawbolt() -> None:
    """Bootstrap prompt should tell the LLM its default identity is Clawbolt."""
    bootstrap = load_prompt("bootstrap")
    assert "Clawbolt" in bootstrap


def test_bootstrap_targets_name_and_timezone() -> None:
    """Bootstrap prompt's only required elicitation is name + timezone (#1050).

    The previous prompt also walked the user through a personality question
    and a rename option. Both were removed to shorten onboarding; this
    test guards against regressions that re-add multi-step elicitation.
    """
    bootstrap = load_prompt("bootstrap").lower()
    assert "name" in bootstrap
    assert "timezone" in bootstrap
    # No personality interrogation in onboarding -- it landed users in a
    # confusing meta-conversation. Removed in #1044.
    assert "how do you like to talk" not in bootstrap
    assert "how do you want me to talk" not in bootstrap


def test_default_soul_includes_clawbolt_name() -> None:
    """Default soul template should identify as Clawbolt."""
    soul = load_prompt("default_soul")
    assert "Clawbolt" in soul
