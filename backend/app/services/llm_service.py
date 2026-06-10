"""LLM service utilities: provider enumeration, model listing, and caching."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from any_llm import LLMProvider, alist_models

from backend.app.config import settings
from backend.app.schemas import ProviderInfo

# Valid reasoning effort levels (matches any_llm.types.completion.ReasoningEffort).
REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "auto")

# Maps reasoning effort level to thinking budget tokens for the Messages API.
_EFFORT_TO_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
}


def reasoning_effort_to_thinking(effort: str) -> dict[str, Any] | None:
    """Convert a reasoning effort level to a Messages API ``thinking`` dict.

    Returns ``None`` for ``"auto"`` (provider default) so callers can skip
    the parameter entirely.
    """
    if not effort or effort == "auto":
        return None
    if effort == "none":
        return {"type": "disabled"}
    budget = _EFFORT_TO_BUDGET.get(effort)
    if budget is not None:
        return {"type": "enabled", "budget_tokens": budget}
    return None


# Providers that run locally (no API key needed).
_LOCAL_PROVIDERS = {"ollama", "llamafile", "llamacpp", "lmstudio", "vllm"}

# Meta-providers that proxy to other providers and should not be directly selectable.
_HIDDEN_PROVIDERS = {"platform", "gateway"}


def get_configured_providers() -> list[ProviderInfo]:
    """Return all known providers. Actual validation happens when listing models."""
    return [
        ProviderInfo(name=p.value, local=p.value in _LOCAL_PROVIDERS)
        for p in LLMProvider
        if p.value not in _HIDDEN_PROVIDERS
    ]


async def get_models(
    provider: str,
    api_key: str | None = None,
    api_base: str | None = None,
) -> list[str]:
    """Fetch available models for a provider."""
    raw = await alist_models(provider=provider, api_key=api_key, api_base=api_base)
    return [m.id if hasattr(m, "id") else str(m) for m in raw]


# ---------------------------------------------------------------------------
# Per-user LLM override resolver
# ---------------------------------------------------------------------------

# Premium (or another plugin) registers a resolver that returns a per-user
# (provider, model) override, or ``None`` when the user has no override
# configured. Either field of the returned tuple may be empty, in which case
# the agent falls back to the global ``settings.llm_provider`` /
# ``settings.llm_model`` value for that field.
UserLLMResolver = Callable[[str], Awaitable[tuple[str, str] | None]]

_user_llm_resolver: UserLLMResolver | None = None


def set_user_llm_resolver(fn: UserLLMResolver | None) -> None:
    """Register an async resolver that returns a per-user (provider, model) override.

    Premium calls this at startup with a function that queries its
    subscription DB. OSS leaves it unset, in which case all users use the
    global ``settings.llm_*`` values.
    """
    global _user_llm_resolver
    _user_llm_resolver = fn


async def resolve_user_llm_override(user_id: str) -> tuple[str, str] | None:
    """Look up a per-user LLM override via the registered resolver, if any.

    Returns ``None`` when no resolver is registered or the resolver
    returns ``None`` for this user. Resolver exceptions are not caught
    here; callers can choose to log-and-fall-through if they want
    defensive behavior.
    """
    if _user_llm_resolver is None:
        return None
    return await _user_llm_resolver(user_id)


# ---------------------------------------------------------------------------
# Prompt caching utilities
# ---------------------------------------------------------------------------


def _cache_control() -> dict[str, Any]:
    """Build the ``cache_control`` block honoring the extended-TTL flag.

    Default Anthropic ephemeral cache TTL is 5 minutes. Users with gaps
    greater than 5 minutes between messages always miss the cache on
    their next turn. Setting ``ttl: "1h"`` extends to 1 hour at a 1.5x
    cache-write premium (vs 1.25x for 5min). Reads are unchanged.
    Providers that do not understand ``ttl`` silently ignore it.
    """
    if settings.llm_cache_extended_ttl:
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def prepare_system_with_caching(system: str) -> list[dict[str, Any]]:
    """Wrap a system prompt string as a single cache-marked content block.

    The whole system string is stable across turns: the agent loop now
    emits dynamic content (memory, cross-session context) after the
    message history rather than in the ``system`` param, so there is no
    dynamic suffix to exclude from the cache (#1420).

    Providers that do not support caching silently ignore the
    ``cache_control`` key.
    """
    return [{"type": "text", "text": system, "cache_control": _cache_control()}]


def apply_history_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stamp a ``cache_control`` breakpoint on the prior-history tail.

    Anthropic caches the prefix up to and including a marked block, so
    marking the last message of the prior conversation history makes that
    history independently cacheable rather than depending on automatic
    prefix caching (which the old dynamic ``system`` suffix broke on every
    memory write, #1420).

    The breakpoint lands on the message immediately before the current
    inbound user turn. The current turn carries volatile content (the
    injected current time and dynamic context) and changes every turn, so
    a breakpoint there would never be read back. The prior history reloads
    byte-identical next turn, so the breakpoint advances forward as the
    conversation grows (the standard rotation).

    The current inbound turn is the last ``user``-role message whose
    content is a plain string; tool-result turns carry list content and
    assistant turns carry block content, so this reliably distinguishes
    it. Returns the list unchanged when there is no prior history to
    cache.
    """
    current_turn_idx: int | None = None
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            current_turn_idx = idx
            break

    if current_turn_idx is None or current_turn_idx == 0:
        return messages

    anchor = messages[current_turn_idx - 1]
    content = anchor.get("content")
    if isinstance(content, str):
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": content, "cache_control": _cache_control()}
        ]
    elif isinstance(content, list) and content:
        blocks = [dict(block) for block in content]
        blocks[-1] = {**blocks[-1], "cache_control": _cache_control()}
    else:
        # Empty or unexpected content shape: nothing safe to mark.
        return messages

    messages[current_turn_idx - 1] = {**anchor, "content": blocks}
    return messages


def apply_in_turn_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stamp a ``cache_control`` breakpoint on a trailing tool-result block.

    During the tool loop, every round re-sends the current user turn
    (which carries the dynamic context: memory, integrations,
    cross-session) plus all prior rounds' tool calls and results as
    uncached input, because the only message-side breakpoint
    (:func:`apply_history_cache_breakpoint`) sits before the current
    turn and never advances within it. With ``max_tool_rounds=10`` and
    large tool results, that cost grows quadratically with round count
    (issue #1430).

    When the request ends in tool results (rounds N > 0), marking the
    last ``tool_result`` block makes the current turn plus rounds
    0..N-1 cacheable for round N; only the newest round's content pays
    cache-write. The message dicts are re-serialized from typed
    messages on every round, so the marker naturally advances with the
    loop instead of accumulating: each request carries at most four
    breakpoints (system, tools, prior-history tail, this one), which is
    Anthropic's limit.

    Round 0 ends in the current user turn (plain string content), not
    tool results, so this is a no-op there and the request keeps three
    breakpoints. Returns the list unchanged when there is nothing safe
    to mark.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if (
        last.get("role") != "user"
        or not isinstance(content, list)
        or not content
        or content[-1].get("type") != "tool_result"
    ):
        return messages
    blocks = [dict(block) for block in content]
    blocks[-1] = {**blocks[-1], "cache_control": _cache_control()}
    messages[-1] = {**last, "content": blocks}
    return messages


def apply_tool_caching(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a cache_control marker to the last tool definition.

    Anthropic caches everything up to and including the marked block, so
    marking the last tool covers the entire tool list. Returns the list
    unchanged when empty.
    """
    if not tools:
        return tools
    tools[-1] = {**tools[-1], "cache_control": _cache_control()}
    return tools
