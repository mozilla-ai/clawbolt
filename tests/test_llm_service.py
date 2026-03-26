"""Tests for LLM service caching utilities."""

from __future__ import annotations

from backend.app.services.llm_service import apply_tool_caching, prepare_system_with_caching


def test_prepare_system_with_caching_returns_content_block() -> None:
    """prepare_system_with_caching wraps a string in a cache-marked content block."""
    result = prepare_system_with_caching("You are a helpful assistant.")
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "You are a helpful assistant."
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_prepare_system_with_caching_preserves_content() -> None:
    """The original system prompt text is preserved exactly."""
    long_prompt = "A" * 5000
    result = prepare_system_with_caching(long_prompt)
    assert result[0]["text"] == long_prompt


def test_apply_tool_caching_marks_last_tool() -> None:
    """apply_tool_caching adds cache_control to only the last tool."""
    tools = [
        {"name": "tool_a", "description": "First tool", "input_schema": {}},
        {"name": "tool_b", "description": "Second tool", "input_schema": {}},
        {"name": "tool_c", "description": "Third tool", "input_schema": {}},
    ]
    result = apply_tool_caching(tools)
    assert len(result) == 3
    assert "cache_control" not in result[0]
    assert "cache_control" not in result[1]
    assert result[2]["cache_control"] == {"type": "ephemeral"}


def test_apply_tool_caching_single_tool() -> None:
    """apply_tool_caching works with a single tool."""
    tools = [{"name": "only_tool", "description": "Solo", "input_schema": {}}]
    result = apply_tool_caching(tools)
    assert result[0]["cache_control"] == {"type": "ephemeral"}
    assert result[0]["name"] == "only_tool"


def test_apply_tool_caching_empty_list() -> None:
    """apply_tool_caching returns empty list unchanged."""
    result = apply_tool_caching([])
    assert result == []


def test_apply_tool_caching_does_not_mutate_original() -> None:
    """apply_tool_caching should not modify the original tool dicts."""
    original = {"name": "tool_a", "description": "A tool", "input_schema": {}}
    tools = [original]
    result = apply_tool_caching(tools)
    # The result's last element should have cache_control
    assert "cache_control" in result[0]
    # But the original dict should be unmodified
    assert "cache_control" not in original
