"""Tests for LLM service caching utilities."""

from __future__ import annotations

from unittest.mock import patch

from backend.app.services.llm_service import apply_tool_caching, prepare_system_with_caching


def test_prepare_system_with_caching_returns_content_block() -> None:
    """prepare_system_with_caching wraps a string in a cache-marked content block."""
    result = prepare_system_with_caching("You are a helpful assistant.")
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "You are a helpful assistant."
    # cache_control is present; TTL field is asserted in dedicated tests below.
    assert result[0]["cache_control"]["type"] == "ephemeral"


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
    assert result[2]["cache_control"]["type"] == "ephemeral"


def test_apply_tool_caching_single_tool() -> None:
    """apply_tool_caching works with a single tool."""
    tools = [{"name": "only_tool", "description": "Solo", "input_schema": {}}]
    result = apply_tool_caching(tools)
    assert result[0]["cache_control"]["type"] == "ephemeral"
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


# ---------------------------------------------------------------------------
# Extended-TTL behavior (#1084)
# ---------------------------------------------------------------------------


def test_prepare_system_uses_1h_ttl_by_default() -> None:
    """Default ``llm_cache_extended_ttl=True`` means cache entries get the
    1-hour TTL rather than the 5-minute Anthropic default.

    Reason: in production we observed 0% cache hit ratio on the first
    turn after any user gap >5 min, because the ephemeral cache had
    expired. Switching to 1h TTL covers typical re-engage windows.
    """
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = True
        result = prepare_system_with_caching("hello")
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_prepare_system_falls_back_to_5min_when_disabled() -> None:
    """Setting ``llm_cache_extended_ttl=False`` opts back into the
    default Anthropic 5-minute TTL. Provided as an escape hatch in case
    a non-Anthropic provider rejects the ttl field."""
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = False
        result = prepare_system_with_caching("hello")
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_apply_tool_caching_uses_1h_ttl_by_default() -> None:
    """Tool list cache_control marker also picks up the extended TTL."""
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = True
        result = apply_tool_caching(
            [{"name": "t", "description": "", "input_schema": {}}],
        )
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_apply_tool_caching_falls_back_to_5min_when_disabled() -> None:
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = False
        result = apply_tool_caching(
            [{"name": "t", "description": "", "input_schema": {}}],
        )
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_prepare_system_with_cache_boundary_marks_only_stable_prefix() -> None:
    """When a CACHE_BOUNDARY marker is present, only the stable prefix
    block carries cache_control; the dynamic suffix block has no marker
    so per-turn variation does not bust the cache."""
    text = "stable prefix\n<!-- CACHE_BOUNDARY -->\ndynamic suffix"
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = True
        result = prepare_system_with_caching(text)
    assert len(result) == 2
    assert result[0]["text"] == "stable prefix"
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert result[1]["text"] == "dynamic suffix"
    assert "cache_control" not in result[1]
