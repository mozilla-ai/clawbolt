"""LLM service utilities: provider enumeration, model listing, and caching."""

from __future__ import annotations

from typing import Any

from any_llm import LLMProvider, alist_models

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
# Prompt caching utilities
# ---------------------------------------------------------------------------


_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"


def prepare_system_with_caching(system: str) -> list[dict[str, Any]]:
    """Wrap a system prompt string as content blocks with cache_control.

    If the prompt contains a ``<!-- CACHE_BOUNDARY -->`` marker (inserted
    by ``SystemPromptBuilder``), the text before the marker is cached and
    the text after it is sent without caching.  This allows the stable
    prefix (identity, instructions) to be reused across turns even when
    dynamic sections (memory, cross-session context) change.

    Providers that do not support caching silently ignore the
    ``cache_control`` key.
    """
    if _CACHE_BOUNDARY in system:
        stable, dynamic = system.split(_CACHE_BOUNDARY, 1)
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": stable.strip(), "cache_control": {"type": "ephemeral"}},
        ]
        dynamic = dynamic.strip()
        if dynamic:
            blocks.append({"type": "text", "text": dynamic})
        return blocks
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def apply_tool_caching(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a cache_control marker to the last tool definition.

    Anthropic caches everything up to and including the marked block, so
    marking the last tool covers the entire tool list. Returns the list
    unchanged when empty.
    """
    if not tools:
        return tools
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools
