"""Tests for LLM service caching utilities."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from any_llm.exceptions import ProviderError

from backend.app.services.llm_service import (
    _cache_control,
    apply_history_cache_breakpoint,
    apply_in_turn_cache_breakpoint,
    apply_tool_caching,
    get_models,
    prepare_system_with_caching,
    provider_honors_cache_control,
    resolve_user_llm_override,
    set_user_llm_resolver,
)

# Any provider that serves the Messages API natively, so the markers are stamped.
_CACHING_PROVIDER = "anthropic"


def test_prepare_system_with_caching_returns_content_block() -> None:
    """prepare_system_with_caching wraps a string in a cache-marked content block."""
    result = prepare_system_with_caching("You are a helpful assistant.", _CACHING_PROVIDER)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "You are a helpful assistant."
    # cache_control is present; TTL field is asserted in dedicated tests below.
    assert result[0]["cache_control"]["type"] == "ephemeral"


def test_prepare_system_with_caching_preserves_content() -> None:
    """The original system prompt text is preserved exactly."""
    long_prompt = "A" * 5000
    result = prepare_system_with_caching(long_prompt, _CACHING_PROVIDER)
    assert isinstance(result, list)
    assert result[0]["text"] == long_prompt


def test_apply_tool_caching_marks_last_tool() -> None:
    """apply_tool_caching adds cache_control to only the last tool."""
    tools = [
        {"name": "tool_a", "description": "First tool", "input_schema": {}},
        {"name": "tool_b", "description": "Second tool", "input_schema": {}},
        {"name": "tool_c", "description": "Third tool", "input_schema": {}},
    ]
    result = apply_tool_caching(tools, _CACHING_PROVIDER)
    assert len(result) == 3
    assert "cache_control" not in result[0]
    assert "cache_control" not in result[1]
    assert result[2]["cache_control"]["type"] == "ephemeral"


def test_apply_tool_caching_single_tool() -> None:
    """apply_tool_caching works with a single tool."""
    tools = [{"name": "only_tool", "description": "Solo", "input_schema": {}}]
    result = apply_tool_caching(tools, _CACHING_PROVIDER)
    assert result[0]["cache_control"]["type"] == "ephemeral"
    assert result[0]["name"] == "only_tool"


def test_apply_tool_caching_empty_list() -> None:
    """apply_tool_caching returns empty list unchanged."""
    result = apply_tool_caching([], _CACHING_PROVIDER)
    assert result == []


def test_apply_tool_caching_does_not_mutate_original() -> None:
    """apply_tool_caching should not modify the original tool dicts."""
    original = {"name": "tool_a", "description": "A tool", "input_schema": {}}
    tools = [original]
    result = apply_tool_caching(tools, _CACHING_PROVIDER)
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
        result = prepare_system_with_caching("hello", _CACHING_PROVIDER)
    assert isinstance(result, list)
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_prepare_system_falls_back_to_5min_when_disabled() -> None:
    """Setting ``llm_cache_extended_ttl=False`` opts back into the
    default Anthropic 5-minute TTL. Provided as an escape hatch in case
    a non-Anthropic provider rejects the ttl field."""
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = False
        result = prepare_system_with_caching("hello", _CACHING_PROVIDER)
    assert isinstance(result, list)
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_apply_tool_caching_uses_1h_ttl_by_default() -> None:
    """Tool list cache_control marker also picks up the extended TTL."""
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = True
        result = apply_tool_caching(
            [{"name": "t", "description": "", "input_schema": {}}],
            _CACHING_PROVIDER,
        )
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_apply_tool_caching_falls_back_to_5min_when_disabled() -> None:
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = False
        result = apply_tool_caching(
            [{"name": "t", "description": "", "input_schema": {}}],
            _CACHING_PROVIDER,
        )
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_prepare_system_wraps_whole_string_in_one_cached_block() -> None:
    """The whole system string is stable now (dynamic content moved to the
    user turn, #1420), so it is a single cache-marked block."""
    text = "stable prefix\n\ndynamic suffix"
    with patch("backend.app.services.llm_service.settings") as mock_settings:
        mock_settings.llm_cache_extended_ttl = True
        result = prepare_system_with_caching(text, _CACHING_PROVIDER)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["text"] == text
    assert result[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


class TestApplyHistoryCacheBreakpoint:
    """The history breakpoint lands on the message before the current turn."""

    def _control(self) -> dict[str, object]:
        return _cache_control()

    def test_marks_message_before_current_turn(self) -> None:
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": "current turn with time + dynamic"},
        ]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        # Breakpoint stamped on the assistant message (index 1), not the
        # volatile current turn (index 2).
        assert result[1]["content"][-1]["cache_control"] == self._control()
        assert isinstance(result[2]["content"], str)
        assert "cache_control" not in result[2]

    def test_converts_string_anchor_to_block(self) -> None:
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "user", "content": "current turn"},
        ]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        anchor = result[0]
        assert isinstance(anchor["content"], list)
        assert anchor["content"][0]["text"] == "older question"
        assert anchor["content"][0]["cache_control"] == self._control()

    def test_marks_last_block_of_tool_result_anchor(self) -> None:
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "calling tool"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "one"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "two"},
                ],
            },
            {"role": "user", "content": "current turn"},
        ]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        tool_results = result[1]["content"]
        assert "cache_control" not in tool_results[0]
        assert tool_results[1]["cache_control"] == self._control()

    def test_no_breakpoint_without_prior_history(self) -> None:
        messages = [{"role": "user", "content": "only the current turn"}]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        assert result == messages
        assert "cache_control" not in result[0]

    def test_no_breakpoint_when_no_string_user_turn(self) -> None:
        # Only tool-result (list-content) user messages: no current inbound
        # string turn to anchor against.
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}],
            },
        ]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        assert all("cache_control" not in block for block in result[0]["content"])
        assert all("cache_control" not in block for block in result[1]["content"])


class TestApplyInTurnCacheBreakpoint:
    """The in-turn breakpoint advances with the tool loop (issue #1430)."""

    def _control(self) -> dict[str, object]:
        return _cache_control()

    def test_marks_trailing_tool_result_block(self) -> None:
        messages = [
            {"role": "user", "content": "current turn"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "one"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "two"},
                ],
            },
        ]
        result = apply_in_turn_cache_breakpoint(messages, _CACHING_PROVIDER)
        tool_results = result[-1]["content"]
        assert "cache_control" not in tool_results[0]
        assert tool_results[1]["cache_control"] == self._control()

    def test_noop_on_round_zero(self) -> None:
        # Round 0 ends in the current user turn (string content).
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": "current turn"},
        ]
        result = apply_in_turn_cache_breakpoint(messages, _CACHING_PROVIDER)
        assert isinstance(result[-1]["content"], str)
        assert "cache_control" not in result[-1]

    def test_noop_on_trailing_assistant_message(self) -> None:
        messages = [
            {"role": "user", "content": "current turn"},
            {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        ]
        result = apply_in_turn_cache_breakpoint(messages, _CACHING_PROVIDER)
        assert all("cache_control" not in block for block in result[-1]["content"])

    def test_noop_on_empty_list(self) -> None:
        assert apply_in_turn_cache_breakpoint([], _CACHING_PROVIDER) == []

    def test_at_most_four_breakpoints_with_history_anchor(self) -> None:
        """Combined with the history anchor, the message side carries at
        most two breakpoints; system and tools carry the other two, which
        is Anthropic's four-breakpoint limit.
        """
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": "current turn"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}],
            },
        ]
        result = apply_history_cache_breakpoint(messages, _CACHING_PROVIDER)
        result = apply_in_turn_cache_breakpoint(result, _CACHING_PROVIDER)

        marker_count = 0
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                marker_count += sum(1 for block in content if "cache_control" in block)
        assert marker_count == 2


# ---------------------------------------------------------------------------
# Per-user LLM override resolver hook (premium plug-point)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_user_llm_resolver() -> Generator[None]:
    """Each test starts with no resolver installed and resets afterwards.

    OSS code under test must not leak resolver state across tests.
    """
    set_user_llm_resolver(None)
    yield
    set_user_llm_resolver(None)


async def test_resolve_user_llm_override_returns_none_when_no_resolver() -> None:
    """With no resolver installed, every user falls through to global settings."""
    assert await resolve_user_llm_override("user-123") is None


async def test_resolve_user_llm_override_calls_registered_resolver() -> None:
    """Installed resolver is invoked with the user_id and its result is returned."""
    received: list[str] = []

    async def fake_resolver(user_id: str) -> tuple[str, str] | None:
        received.append(user_id)
        return ("anthropic", "claude-haiku-4-5")

    set_user_llm_resolver(fake_resolver)
    result = await resolve_user_llm_override("user-abc")
    assert result == ("anthropic", "claude-haiku-4-5")
    assert received == ["user-abc"]


async def test_resolve_user_llm_override_passes_through_none() -> None:
    """Resolver may return None to indicate "no override for this user"."""

    async def fake_resolver(_: str) -> tuple[str, str] | None:
        return None

    set_user_llm_resolver(fake_resolver)
    assert await resolve_user_llm_override("user-xyz") is None


# ---------------------------------------------------------------------------
# get_models: preserve the "provider cannot list models" signal
# ---------------------------------------------------------------------------


async def test_get_models_unwraps_not_implemented_from_unified_error() -> None:
    """A provider that cannot enumerate models must still raise NotImplementedError.

    any-llm raises ``NotImplementedError`` from inside its ``handle_exceptions``
    decorator, so with ``ANY_LLM_UNIFIED_EXCEPTIONS`` enabled it arrives wrapped
    in a ``ProviderError``. Callers (the OSS profile endpoint and the premium
    admin LLM picker) branch on ``NotImplementedError`` to render a free-text
    model field instead of a listing failure, so the signal has to survive.
    """
    raw = NotImplementedError("Provider doesn't support listing models.")
    wrapped = ProviderError(
        message="Provider doesn't support listing models.",
        original_exception=raw,
        provider_name="voyage",
    )
    with (
        patch("backend.app.services.llm_service.alist_models", side_effect=wrapped),
        pytest.raises(NotImplementedError),
    ):
        await get_models("voyage")


async def test_get_models_reraises_other_unified_errors() -> None:
    """A real listing failure is not mistaken for "unsupported"."""
    wrapped = ProviderError(
        message="upstream 503",
        original_exception=RuntimeError("upstream 503"),
        provider_name="openai",
    )
    with (
        patch("backend.app.services.llm_service.alist_models", side_effect=wrapped),
        pytest.raises(ProviderError),
    ):
        await get_models("openai")


# ---------------------------------------------------------------------------
# Cache markers are gated on the provider honoring them (any-llm#1228)
# ---------------------------------------------------------------------------


class TestCacheControlProviderGate:
    """Only providers that serve the Messages API natively get cache markers.

    Every other provider reaches the wire through any-llm's
    Messages-to-Completions bridge. That bridge rebuilds user, assistant and
    tool blocks (dropping their markers) but forwards a block-list ``system``
    verbatim into ``messages[0].content``, which a strict OpenAI-compatible
    backend rejects with a 400. Stamping a marker such a provider cannot honor
    is therefore not a harmless no-op, it breaks the request.
    """

    @pytest.mark.parametrize("provider", ["anthropic", "azureanthropic", "vertexaianthropic"])
    def test_native_messages_providers_are_honored(self, provider: str) -> None:
        assert provider_honors_cache_control(provider) is True

    @pytest.mark.parametrize(
        "provider",
        [
            "fireworks",
            "openai",
            "bedrock",
            "ollama",
            # Gateways are excluded on purpose: the downstream provider rides in
            # the model string, so the marker cannot be verified from the name.
            "gateway",
            "mzai",
            "otari",
        ],
    )
    def test_bridge_providers_are_not_honored(self, provider: str) -> None:
        assert provider_honors_cache_control(provider) is False

    def test_provider_match_is_case_insensitive(self) -> None:
        assert provider_honors_cache_control("Anthropic") is True
        assert provider_honors_cache_control("FIREWORKS") is False

    def test_system_stays_a_plain_string_for_bridge_provider(self) -> None:
        """The regression: a block-list system is what Fireworks rejected.

        Fireworks answered 400 "Input should be a valid string, field:
        'messages[0].content.str'" because the block list arrived as the
        converted system message.
        """
        result = prepare_system_with_caching("You are a helpful assistant.", "fireworks")
        assert result == "You are a helpful assistant."
        assert isinstance(result, str)

    def test_history_breakpoint_is_skipped_for_bridge_provider(self) -> None:
        """No marker, and the string anchor is not rewritten into a block list.

        The bridge would only flatten such a rewrite back to content parts, so
        the rewrite buys nothing and widens the shape sent to the provider.
        """
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "user", "content": "current turn"},
        ]
        result = apply_history_cache_breakpoint(messages, "fireworks")
        assert result[0]["content"] == "older question"
        assert isinstance(result[0]["content"], str)

    def test_in_turn_breakpoint_is_skipped_for_bridge_provider(self) -> None:
        messages = [
            {"role": "user", "content": "current turn"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": "one"}],
            },
        ]
        result = apply_in_turn_cache_breakpoint(messages, "fireworks")
        assert all("cache_control" not in block for block in result[-1]["content"])

    def test_tool_caching_is_skipped_for_bridge_provider(self) -> None:
        tools = [{"name": "tool_a", "description": "A tool", "input_schema": {}}]
        result = apply_tool_caching(tools, "fireworks")
        assert "cache_control" not in result[0]

    def test_no_marker_survives_anywhere_for_a_bridge_provider(self) -> None:
        """End to end over all four breakpoints: the request carries none of them."""
        messages = [
            {"role": "user", "content": "older question"},
            {"role": "assistant", "content": [{"type": "text", "text": "older answer"}]},
            {"role": "user", "content": "current turn"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}],
            },
        ]
        result = apply_history_cache_breakpoint(messages, "fireworks")
        result = apply_in_turn_cache_breakpoint(result, "fireworks")
        tools = apply_tool_caching(
            [{"name": "t", "description": "", "input_schema": {}}], "fireworks"
        )
        system = prepare_system_with_caching("system prompt", "fireworks")

        assert isinstance(system, str)
        assert all("cache_control" not in tool for tool in tools)
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                assert all("cache_control" not in block for block in content)
