"""Tests for the prompt loader utility."""

import pytest

from backend.app.agent.prompts import load_prompt

ALL_PROMPT_NAMES = [
    "onboarding",
    "compaction",
    "instructions",
    "proactive",
    "recall",
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
    assert "onboarding" in load_prompt("onboarding") or "user" in load_prompt("onboarding")
