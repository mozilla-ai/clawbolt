"""LLM service utilities: provider enumeration, model listing, and caching."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast, get_args

from anthropic.types import InputJSONDelta, SignatureDelta, TextDelta, ThinkingDelta
from any_llm import AnyLLMError, LLMProvider, alist_models, amessages
from any_llm.exceptions import ProviderError
from any_llm.types.messages import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    MessageContentBlock,
    MessageDelta,
    MessageDeltaEvent,
    MessageDeltaUsage,
    MessageResponse,
    MessageStartEvent,
    MessageStopEvent,
    MessageStreamEvent,
    MessageUsage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from backend.app.config import settings
from backend.app.schemas import ProviderInfo, ReasoningEffort

logger = logging.getLogger(__name__)

# Valid reasoning effort levels (matches any_llm.types.completion.ReasoningEffort).
REASONING_EFFORT_VALUES: tuple[str, ...] = get_args(ReasoningEffort)

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

# Providers whose Anthropic Messages path preserves ``cache_control`` markers.
# Other provider bridges discard the markers, so stamping them adds no value.
_CACHE_CONTROL_PROVIDERS = {"anthropic", "azureanthropic", "vertexaianthropic"}


def provider_honors_cache_control(provider: str) -> bool:
    """Return whether cache markers are enabled and useful for *provider*.

    Answers for a bare provider only. A named endpoint states the answer
    outright in its ``cache_control`` column and consults this just for
    ``auto``; see ``llm_endpoints._cache_control_for``. The base URL is never
    consulted, since it does not reveal a gateway's downstream provider.
    ``llm_prompt_cache='never'`` supports older gateways that reject markers.
    """
    if settings.llm_prompt_cache == "never":
        return False
    return provider.lower() in _CACHE_CONTROL_PROVIDERS


def is_local_provider(provider: str) -> bool:
    """True when *provider* runs on the operator's own machine and needs no key.

    Used to decide whether a caller may choose the endpoint a model listing hits.
    A local provider carries no server-held credential, so pointing one at
    another URL leaks nothing; a hosted provider does, so it must not be
    redirectable by a request parameter.
    """
    return provider.lower() in _LOCAL_PROVIDERS


class ReasoningStyle(StrEnum):
    """How a target wants to be asked for reasoning."""

    THINKING = "thinking"
    """Anthropic's ``thinking`` budget dict. The default for every dialect."""

    EFFORT = "effort"
    """OpenAI's scalar ``reasoning_effort``, forwarded through kwargs."""

    NONE = "none"
    """Send no reasoning parameter at all.

    Not the same as asking for zero reasoning. Some endpoints reject the
    parameter's *presence* in combination with something else in the request
    rather than rejecting its value, so no value works and the only way
    through is to omit the key. The case this was written for refuses
    ``reasoning_effort`` together with function tools on
    ``/v1/chat/completions`` while accepting either one alone.
    """


@dataclass(frozen=True)
class LLMTarget:
    """Everything one ``amessages`` call needs, with nothing left to infer.

    Built by ``services.llm_endpoints.resolve_target``, which is also where
    the reasoning for the type is written down. The short version: a provider
    name stops describing the vendor as soon as a gateway is in the path, so
    a target states the capabilities outright instead of deriving them from
    the name at each call site.
    """

    provider: str
    model: str
    api_base: str | None = None
    api_key: str | None = None
    # Empty when the selection names no endpoint, i.e. a bare provider.
    endpoint: str = ""
    honors_cache_control: bool = False
    reasoning_style: ReasoningStyle = ReasoningStyle.THINKING
    # False when (provider, model) does not identify who billed the tokens,
    # so a cost figure would be fiction. The evaluator reports it rather than
    # quietly showing a zero.
    priced: bool = True

    def connection_kwargs(self) -> dict[str, Any]:
        """The where-and-what half of the call.

        ``api_key`` is omitted when the target carries none, which is what
        lets any-llm resolve the dialect's own environment variable.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "provider": self.provider,
            "api_base": self.api_base,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        return kwargs

    def reasoning_kwargs(self, effort: str) -> dict[str, Any]:
        """The reasoning half, in whichever shape this target accepts.

        ``"auto"`` (or empty) sends nothing and leaves the provider on its
        own default, which is what the ``thinking``-shaped path has always
        done for that value.
        """
        if not effort or effort == "auto":
            return {}
        if self.reasoning_style is ReasoningStyle.NONE:
            return {}
        if self.reasoning_style is ReasoningStyle.EFFORT:
            return {"reasoning_effort": effort}
        thinking = reasoning_effort_to_thinking(effort)
        return {"thinking": thinking} if thinking is not None else {}

    def describe(self) -> str:
        """Human-readable target for logs and health labels."""
        return f"{self.endpoint or self.provider}/{self.model}"


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


@dataclass(frozen=True)
class UserLLMOverride:
    """A per-user pin to a specific endpoint, provider, and/or model.

    Any field may be empty, meaning "use the global default for this one".
    ``endpoint`` supersedes ``provider`` when both are set, for the reason
    given in ``llm_endpoints.resolve_target``.
    """

    endpoint: str = ""
    provider: str = ""
    model: str = ""

    def __bool__(self) -> bool:
        """False when the override pins nothing."""
        return bool(self.endpoint or self.provider or self.model)


# Premium (or another plugin) registers a resolver that returns a per-user
# override, or ``None`` when the user has none configured.
UserLLMResolver = Callable[[str], Awaitable[UserLLMOverride | None]]

_user_llm_resolver: UserLLMResolver | None = None


def set_user_llm_resolver(fn: UserLLMResolver | None) -> None:
    """Register an async resolver that returns a per-user LLM override.

    Premium calls this at startup with a function that queries its
    subscription DB. OSS leaves it unset, in which case all users use the
    global ``settings.llm_*`` values.
    """
    global _user_llm_resolver
    _user_llm_resolver = fn


async def resolve_user_llm_override(user_id: str) -> UserLLMOverride | None:
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


def prepare_system_with_caching(system: str, target: LLMTarget) -> str | list[dict[str, Any]]:
    """Wrap a system prompt string as a single cache-marked content block.

    The whole system string is stable across turns: the agent loop now
    emits dynamic content (memory, cross-session context) after the
    message history rather than in the ``system`` param, so there is no
    dynamic suffix to exclude from the cache (#1420).

    Returns *system* unchanged when *target* cannot honor the marker, which
    keeps a plain string plain rather than wrapping it in a block list that
    any-llm's bridge would only flatten back again. See
    :func:`provider_honors_cache_control`.
    """
    if not target.honors_cache_control:
        return system
    return [{"type": "text", "text": system, "cache_control": _cache_control()}]


def apply_history_cache_breakpoint(
    messages: list[dict[str, Any]],
    target: LLMTarget,
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
    cache, or when *target* cannot honor the marker.

    Withholding the marker also avoids rewriting a plain-string user message
    into a block list for a target that would only flatten it back again.
    """
    if not target.honors_cache_control:
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
    target: LLMTarget,
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
    to mark, or when *target* cannot honor the marker.
    """
    if not target.honors_cache_control:
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


def apply_tool_caching(tools: list[dict[str, Any]], target: LLMTarget) -> list[dict[str, Any]]:
    """Add a cache_control marker to the last tool definition.

    Anthropic caches everything up to and including the marked block, so
    marking the last tool covers the entire tool list. Returns the list
    unchanged when empty, or when *target* cannot honor the marker.
    """
    if not target.honors_cache_control:
        return tools
    if not tools:
        return tools
    tools[-1] = {**tools[-1], "cache_control": _cache_control()}
    return tools


@dataclass
class _BlockState:
    """Deltas accumulated for one content block index."""

    start: MessageContentBlock
    text: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    json_input: list[str] = field(default_factory=list)
    signature: str = ""

    def build(self) -> MessageContentBlock:
        """Fold the accumulated deltas back into the block they belong to."""
        if isinstance(self.start, TextBlock):
            return self.start.model_copy(update={"text": self.start.text + "".join(self.text)})
        if isinstance(self.start, ThinkingBlock):
            return self.start.model_copy(
                update={
                    "thinking": self.start.thinking + "".join(self.thinking),
                    "signature": self.signature or self.start.signature,
                }
            )
        if isinstance(self.start, ToolUseBlock):
            # ``input`` streams as JSON text fragments that are only valid
            # once concatenated. An empty join is the no-delta case, where
            # the block already carries its input.
            raw = "".join(self.json_input)
            if not raw:
                return self.start
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Tool input JSON did not parse after streaming; keeping it raw")
                return self.start
            return self.start.model_copy(update={"input": parsed})
        return self.start


class _MessageAccumulator:
    """Rebuilds a ``MessageResponse`` from a Messages API event stream.

    Providers with a native Anthropic Messages API attach the finished
    message to ``message_stop``, which is the whole job done for them.
    any-llm's OpenAI-dialect bridge does not, so for those the message is
    reassembled from the individual block and usage events instead.
    """

    def __init__(self) -> None:
        self._final: MessageResponse | None = None
        self._start: MessageResponse | None = None
        self._blocks: dict[int, _BlockState] = {}
        self._delta: MessageDelta | None = None
        self._usage: MessageDeltaUsage | None = None

    def add(self, event: MessageStreamEvent) -> None:
        """Fold one stream event into the accumulated state."""
        if isinstance(event, MessageStartEvent):
            self._start = event.message
        elif isinstance(event, ContentBlockStartEvent):
            self._blocks[event.index] = _BlockState(start=event.content_block)
        elif isinstance(event, ContentBlockDeltaEvent):
            self._add_delta(event)
        elif isinstance(event, MessageDeltaEvent):
            self._delta = event.delta
            self._usage = event.usage
        elif isinstance(event, MessageStopEvent):
            self._final = event.message

    def _add_delta(self, event: ContentBlockDeltaEvent) -> None:
        block = self._blocks.get(event.index)
        if block is None:
            # A delta for an index that never opened means the provider
            # skipped content_block_start. Nothing to attach it to.
            logger.warning("Dropping stream delta for unopened block index %d", event.index)
            return
        delta = event.delta
        if isinstance(delta, TextDelta):
            block.text.append(delta.text)
        elif isinstance(delta, ThinkingDelta):
            block.thinking.append(delta.thinking)
        elif isinstance(delta, SignatureDelta):
            block.signature = delta.signature
        elif isinstance(delta, InputJSONDelta):
            block.json_input.append(delta.partial_json)

    def build(self) -> MessageResponse:
        """Return the accumulated response.

        Raises ``ProviderError`` when the stream carried neither a final
        message nor a ``message_start`` to rebuild from, which is the shape
        a connection cut mid-stream leaves behind.
        """
        if self._final is not None:
            return self._final
        if self._start is None:
            msg = "LLM stream ended without a message to accumulate"
            raise ProviderError(msg)
        content = [self._blocks[i].build() for i in sorted(self._blocks)]
        usage = self._merged_usage(self._start.usage)
        update: dict[str, Any] = {"content": content, "usage": usage}
        if self._delta is not None:
            update["stop_reason"] = self._delta.stop_reason
            update["stop_sequence"] = self._delta.stop_sequence
        return self._start.model_copy(update=update)

    def _merged_usage(self, start: MessageUsage) -> MessageUsage:
        """Overlay the final usage counts onto the ones ``message_start`` gave.

        ``message_start`` reports input tokens before generation begins and
        ``message_delta`` reports the output count at the end, so neither one
        alone is the whole bill.
        """
        if self._usage is None:
            return start
        counts = self._usage.model_dump(exclude_none=True)
        return start.model_copy(update=counts)


async def amessages_streamed(**kwargs: Any) -> MessageResponse:
    """Run a Messages call over SSE and return the accumulated response.

    The result matches what ``amessages`` returns unstreamed. The difference
    is on the wire: bytes arrive throughout the generation instead of only
    once it has finished.

    That is what lets a long generation survive a reverse proxy. Cloudflare
    measures its proxy read timeout against silence on the connection, so an
    unstreamed call that outruns the timeout is cut off with a 524 even
    though the origin is healthy and answers moments later. The origin bills
    for the response regardless, so the unstreamed shape costs money and
    returns nothing.
    """
    stream = cast(
        "AsyncIterator[MessageStreamEvent]",
        await amessages(stream=True, **kwargs),
    )
    accumulator = _MessageAccumulator()
    async for event in stream:
        accumulator.add(event)
    return accumulator.build()
