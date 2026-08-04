"""LLM service utilities: provider enumeration, model listing, and caching."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from any_llm import AnyLLMError, LLMProvider, alist_models

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

# Providers that serve the Anthropic Messages API natively, so a ``cache_control``
# marker survives to the wire. Every other provider goes through any-llm's
# Messages-to-Completions bridge, which rebuilds user, assistant and tool blocks
# (silently dropping their markers) and forwards a block-list ``system`` verbatim
# into ``messages[0].content``, where a strict OpenAI-compatible backend rejects
# the whole request (Fireworks answers 400 "Input should be a valid string,
# field: 'messages[0].content.str'"). See any-llm#1228.
#
# Gateway providers are deliberately absent even though any-llm's ``otari``
# provider forwards Messages natively: whether the markers are honored then
# depends on the gateway's downstream provider, which is encoded in the model
# string rather than the provider name. Sending markers we cannot verify fails
# closed as a 400, while omitting them only forgoes caching, so the fail-safe
# choice is to omit.
#
# Membership here is necessary but not sufficient: ``anthropic`` also has to be
# reaching Anthropic. See :func:`_api_base_reaches_anthropic`.
_CACHE_CONTROL_PROVIDERS = {"anthropic", "azureanthropic", "vertexaianthropic"}

# Registrable domain any-llm's ``anthropic`` provider talks to when no
# ``llm_api_base`` is configured.
_ANTHROPIC_HOST = "anthropic.com"


def _api_base_reaches_anthropic(api_base: str | None) -> bool:
    """True when an ``anthropic`` request goes to Anthropic and not an intermediary.

    An unset base is Anthropic's own API, which any-llm supplies as the default.
    Anything else is a proxy or a gateway, and a gateway's real downstream
    provider rides in the model string rather than the provider name, so it is
    invisible here: the configured provider reads ``anthropic`` whether the model
    behind the gateway is Claude or a Fireworks-hosted DeepSeek. The second case
    400s on a marked request, so an unrecognized base is treated as unverifiable.

    Only the plain ``anthropic`` provider is subject to this. The Azure and Vertex
    members of :data:`_CACHE_CONTROL_PROVIDERS` always carry a base naming the
    operator's own resource, so a base being set there is ordinary configuration
    and says nothing about an intermediary.
    """
    if not api_base:
        return True
    host = urlparse(api_base if "://" in api_base else f"https://{api_base}").hostname or ""
    return host == _ANTHROPIC_HOST or host.endswith(f".{_ANTHROPIC_HOST}")


def provider_honors_cache_control(provider: str) -> bool:
    """True when a ``cache_control`` marker reaches the wire intact for *provider*.

    Gates every cache breakpoint the agent stamps. Marking a prompt that cannot
    honor it is never merely wasteful: the ``system`` breakpoint makes the request
    unserializable for a strict OpenAI-compatible backend, which answers 400
    rather than ignoring the marker, so an unverifiable marker has to be withheld.
    Withholding one only forgoes caching, which is why that is the fail-safe side.

    ``llm_prompt_cache`` overrides the endpoint half of the decision: ``"always"``
    marks through a custom base, ``"never"`` disables caching outright. Neither
    can mark for a provider that reaches the wire through the bridge.
    """
    if settings.llm_prompt_cache == "never":
        return False
    normalized = provider.lower()
    if normalized not in _CACHE_CONTROL_PROVIDERS:
        return False
    if normalized != "anthropic" or settings.llm_prompt_cache == "always":
        return True
    return _api_base_reaches_anthropic(settings.llm_api_base)


def is_local_provider(provider: str) -> bool:
    """True when *provider* runs on the operator's own machine and needs no key.

    Used to decide whether a caller may choose the endpoint a model listing hits.
    A local provider carries no server-held credential, so pointing one at
    another URL leaks nothing; a hosted provider does, so it must not be
    redirectable by a request parameter.
    """
    return provider.lower() in _LOCAL_PROVIDERS


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
    """Fetch available models for a provider.

    "This provider cannot enumerate models" surfaces as ``NotImplementedError``,
    which callers branch on to render a free-text model field instead of an
    error. any-llm raises that from inside its ``handle_exceptions`` decorator,
    so with ``ANY_LLM_UNIFIED_EXCEPTIONS`` enabled (see ``config.py``) it arrives
    wrapped in a ``ProviderError`` and the distinction is lost. Unwrap it so the
    "unsupported" signal survives the conversion.
    """
    try:
        raw = await alist_models(provider=provider, api_key=api_key, api_base=api_base)
    except AnyLLMError as exc:
        if isinstance(exc.original_exception, NotImplementedError):
            raise exc.original_exception from exc
        raise
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


def prepare_system_with_caching(system: str, provider: str) -> str | list[dict[str, Any]]:
    """Wrap a system prompt string as a single cache-marked content block.

    The whole system string is stable across turns: the agent loop now
    emits dynamic content (memory, cross-session context) after the
    message history rather than in the ``system`` param, so there is no
    dynamic suffix to exclude from the cache (#1420).

    Returns *system* unchanged when *provider* cannot honor the marker. A
    provider that does not serve the Messages API natively does not merely
    ignore the ``cache_control`` key: the block-list shape itself reaches the
    provider as ``messages[0].content`` and a strict OpenAI-compatible backend
    rejects the request outright. See :func:`provider_honors_cache_control`.
    """
    if not provider_honors_cache_control(provider):
        return system
    return [{"type": "text", "text": system, "cache_control": _cache_control()}]


def apply_history_cache_breakpoint(
    messages: list[dict[str, Any]],
    provider: str,
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
    cache, or when *provider* cannot honor the marker.

    Withholding the marker also avoids rewriting a plain-string user message
    into a block list for a provider that would only flatten it back again.
    """
    if not provider_honors_cache_control(provider):
        return messages

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
    provider: str,
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
    to mark, or when *provider* cannot honor the marker.
    """
    if not provider_honors_cache_control(provider):
        return messages
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


def apply_tool_caching(tools: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Add a cache_control marker to the last tool definition.

    Anthropic caches everything up to and including the marked block, so
    marking the last tool covers the entire tool list. Returns the list
    unchanged when empty, or when *provider* cannot honor the marker.
    """
    if not provider_honors_cache_control(provider):
        return tools
    if not tools:
        return tools
    tools[-1] = {**tools[-1], "cache_control": _cache_control()}
    return tools
