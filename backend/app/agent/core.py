import asyncio
import copy
import json
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from any_llm import (
    AuthenticationError,
    ContentFilterError,
    ContextLengthExceededError,
    RateLimitError,
    amessages,
)
from any_llm.types.messages import MessageResponse
from pydantic import ValidationError

from backend.app.agent.approval import (
    ApprovalDecision,
    PermissionLevel,
    format_approval_message,
    get_approval_gate,
    get_approval_store,
)
from backend.app.agent.context import StoredToolInteraction, StoredToolReceipt
from backend.app.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from backend.app.agent.llm_parsing import (
    ParsedToolCall,
    get_response_text,
    get_response_thinking,
    parse_tool_calls,
)
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
    messages_to_messages_api,
)
from backend.app.agent.observer import (
    PURPOSE_AGENT_FOLLOWUP,
    PURPOSE_AGENT_MAIN,
    LLMRequestPayload,
    LLMResponsePayload,
    compute_min_message_seq,
    emit_llm_request,
    emit_llm_response,
)
from backend.app.agent.system_prompt import (
    build_agent_system_prompt_parts,
    build_time_user_context,
)
from backend.app.agent.tool_errors import (
    _DEFAULT_ERROR_HINT,
    _ERROR_KIND_HINTS,
    _TRUNCATION_HINT,
    build_error_hint,
    format_validation_error,
)
from backend.app.agent.tools.base import (
    Tool,
    ToolErrorKind,
    ToolTags,
    tool_to_function_schema,
)
from backend.app.agent.tools.registry import ToolContext, ToolRegistry
from backend.app.agent.trimming import trim_messages
from backend.app.config import settings
from backend.app.logging_utils import mask_pii
from backend.app.models import User
from backend.app.services.llm_service import (
    apply_history_cache_breakpoint,
    apply_in_turn_cache_breakpoint,
    apply_tool_caching,
    prepare_system_with_caching,
    reasoning_effort_to_thinking,
)
from backend.app.services.llm_usage import log_llm_usage

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = settings.max_tool_rounds
LLM_MAX_RETRIES = settings.llm_max_retries

# Conservative default; most models support 128K+ but we leave room for output
MAX_INPUT_TOKENS = settings.max_input_tokens

# Stop reasons that represent a valid, non-error LLM response.
# Anything outside this set indicates a provider-level error and the
# response should *not* be persisted to session history to avoid
# context poisoning.
_VALID_STOP_REASONS: set[str | None] = {"end_turn", "max_tokens", "tool_use", "stop_sequence", None}

_LLM_ERROR_FALLBACK = "I'm having trouble thinking right now. Can you try again in a moment?"

# Last API-reported input token count per user, surviving across agent
# instances. A fresh ClawboltAgent is constructed per message, so without
# this the proactive trim at the top of ``process_message`` always falls
# back to the chars/4 + flat-overhead heuristic, which ignores tool
# schemas and the real system prompt size and therefore fires later than
# configured (issue #1433). Process-local by design: after a restart the
# first message per user falls back to the heuristic once, then the real
# count takes over. Bounded LRU so a multi-tenant deployment cannot grow
# it without limit.
_LAST_INPUT_TOKENS: OrderedDict[str, int] = OrderedDict()
_LAST_INPUT_TOKENS_MAX = 1024


def _remember_input_tokens(user_id: str, tokens: int) -> None:
    """Record the latest API-reported input token count for *user_id*."""
    _LAST_INPUT_TOKENS[user_id] = tokens
    _LAST_INPUT_TOKENS.move_to_end(user_id)
    while len(_LAST_INPUT_TOKENS) > _LAST_INPUT_TOKENS_MAX:
        _LAST_INPUT_TOKENS.popitem(last=False)


def _recall_input_tokens(user_id: str) -> int | None:
    """Return the last reported input token count for *user_id*, if any."""
    return _LAST_INPUT_TOKENS.get(user_id)


def reset_last_input_tokens() -> None:
    """Clear the per-user input-token cache (for tests)."""
    _LAST_INPUT_TOKENS.clear()


# Patterns that commonly appear in tool exception messages and would leak
# secrets into the LLM context (and thence into provider logs and the
# `messages` table) if echoed verbatim. Add new vendors here as they show up.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "Bearer ***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_***"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"), "xox-***"),
    (re.compile(r"(?i)\b(api[_\-]?key|password|token|secret)\s*[:=]\s*\S+"), r"\1=***"),
)


def _scrub_secrets(text: str) -> str:
    """Strip well-known secret formats so we can safely echo errors to the LLM."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_ToolEntry = tuple[int, Tool, dict[str, Any]]
"""(parsed_calls index, Tool, validated args). Threaded through the per-turn
execution pipeline by validation, approval, and the parallel scheduler."""


def _normalize_tool_args(args: dict[str, Any]) -> str:
    """Canonical string form of validated tool args for duplicate detection.

    Stable across dict iteration order so the same logical call always
    hashes to the same key, regardless of how the LLM emitted the keys.
    ``default=str`` so an unexpected non-JSON value cannot crash the
    telemetry path; the goal here is a diagnostic signal, not a perfect
    encoder.
    """
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(args.items()))


def _stringify_numbers_for_string_fields(
    tool_args: dict[str, Any], exc: ValidationError
) -> dict[str, Any] | None:
    """Stringify numeric values that failed ``str`` field validation.

    LLMs emit numeric-looking values (work order numbers, street numbers,
    event titles like "20240") as JSON numbers. Pydantic v2 rejects
    ``int``/``float`` input for ``str`` fields by default, which fails the
    whole tool call even though the intent is unambiguous. Returns a copy
    of ``tool_args`` with each offending value stringified, or ``None``
    when no error is coercible (the original error should be reported).
    Booleans are left alone: ``True`` for a string field is a real bug,
    not a serialization quirk.
    """
    coerced: Any = copy.deepcopy(tool_args)
    fixed_any = False
    for error in exc.errors():
        if error.get("type") != "string_type":
            continue
        value = error.get("input")
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        container: Any = coerced
        loc = error.get("loc", ())
        try:
            for key in loc[:-1]:
                container = container[key]
            cast("dict[Any, Any]", container)[loc[-1]] = str(value)
        except (KeyError, IndexError, TypeError):
            continue
        fixed_any = True
    return coerced if fixed_any else None


def _resolve_concurrency_group(tool: Tool, validated_args: dict[str, Any]) -> str | None:
    """Resolve a tool's concurrency group for a specific call.

    ``Tool.concurrency_group`` may be a static string, a callable that
    derives a key from the call's validated args, or ``None``. The
    callable form mirrors ``ApprovalPolicy.resource_extractor`` and lets a
    single Tool route distinct calls to distinct serialization buckets
    (e.g. a workspace writer keyed by file path).
    """
    group = tool.concurrency_group
    if group is None or isinstance(group, str):
        return group
    return group(validated_args)


def _bucket_by_concurrency_group(
    approved_entries: list[_ToolEntry],
) -> list[list[tuple[int, _ToolEntry]]]:
    """Bucket approved entries into schedule units for concurrent execution.

    Each entry whose resolved concurrency group is ``None`` becomes its
    own (parallel) unit. Entries sharing a non-None group are grouped
    into a single sequential unit. Position within a non-None bucket
    follows the order entries appear in ``approved_entries``, which the
    scheduler honors when running the bucket sequentially.

    Pure-functional so the bucketing rule can be exercised without
    standing up an agent.
    """
    buckets: dict[str | None, list[tuple[int, _ToolEntry]]] = {}
    for pos, entry in enumerate(approved_entries):
        _idx, tool_obj, validated_args = entry
        key = _resolve_concurrency_group(tool_obj, validated_args)
        buckets.setdefault(key, []).append((pos, entry))

    units: list[list[tuple[int, _ToolEntry]]] = []
    for group_key, items in buckets.items():
        if group_key is None:
            units.extend([item] for item in items)
        else:
            units.append(items)
    return units


@dataclass
class AgentResponse:
    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    memories_saved: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[StoredToolInteraction] = field(default_factory=list)
    is_error_fallback: bool = False
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_input_tokens: int = 0
    total_cache_read_input_tokens: int = 0
    system_prompt: str = ""
    # The exact body that was published to the outbound bus, including any
    # receipt block appended by ``append_receipts``. Set by
    # ``dispatch_reply_step`` so ``persist_outbound`` can store the user-facing
    # text instead of just ``reply_text`` (the LLM's prose, pre-receipts).
    dispatched_body: str = ""
    # Concatenated extended-thinking text from the FINAL LLM response in the
    # agent loop, captured from any ``ThinkingBlock`` content. Empty when the
    # provider did not return thinking blocks (thinking disabled, or
    # provider does not support extended thinking). ``persist_outbound``
    # writes it onto the assistant message so admins can audit "why did
    # the agent reply this way" without re-querying the LLM.
    thinking_text: str = ""


class ClawboltAgent:
    """Main agent that processes user messages and produces actions."""

    def __init__(
        self,
        user: User,
        channel: str = "",
        publish_outbound: Callable[[Any], Awaitable[None]] | None = None,
        chat_id: str | None = None,
        tool_context: ToolContext | None = None,
        registry: ToolRegistry | None = None,
        session_id: str = "",
        excluded_tool_names: set[str] | None = None,
        request_id: str = "",
        llm_provider_override: str = "",
        llm_model_override: str = "",
    ) -> None:
        self.user = user
        self._channel = channel
        self._publish_outbound = publish_outbound
        self._chat_id = chat_id
        self.tools: list[Tool] = []
        self._tools_by_name: dict[str, Tool] = {}
        self._subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = []
        self._tool_context = tool_context
        self._registry = registry
        self._last_input_tokens: int = 0
        self._session_id = session_id
        self._excluded_tool_names = excluded_tool_names
        self._request_id = request_id
        self._llm_provider_override = llm_provider_override
        self._llm_model_override = llm_model_override
        # Previous round's tool-name sequence, used to detect when a newly
        # built tool list fails to preserve the prefix (which busts the
        # Anthropic tools prompt cache). The list is append-only by design;
        # a non-monotonic change is logged as a warning.
        self._prev_tool_names: list[str] = []
        # Cached Anthropic tool schemas keyed by the tool-name sequence that
        # produced them. Reused across rounds to avoid recomputing identical
        # JSON schemas every turn.
        self._cached_tool_schemas: list[dict[str, Any]] | None = None
        self._reactive_trim_dropped: list[AgentMessage] = []
        # Remembers ASK decisions within a single agent run so the user is not
        # re-prompted for the same (tool, resource) if the LLM retries or
        # chains calls. ALWAYS_ALLOW is persisted to PERMISSIONS.json and does
        # not need this cache.
        self._approval_cache: dict[tuple[str, str | None], ApprovalDecision] = {}

    def subscribe(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        """Register an event subscriber.

        The callback is invoked with each ``AgentEvent`` during processing.
        Multiple subscribers are supported and called in registration order.
        """
        self._subscribers.append(callback)

    async def _emit(self, event: AgentEvent) -> None:
        """Notify all subscribers of an event.  Errors are logged, not raised."""
        for cb in self._subscribers:
            try:
                await cb(event)
            except Exception:
                logger.exception("Event subscriber error for %s", type(event).__name__)

    async def _send_typing_indicator(self) -> None:
        """Send a typing indicator via the bus if a publish callback and chat_id are available."""
        if self._publish_outbound and self._chat_id and self._channel:
            try:
                from backend.app.bus import OutboundMessage

                await self._publish_outbound(
                    OutboundMessage(
                        channel=self._channel,
                        chat_id=self._chat_id,
                        content="",
                        is_typing_indicator=True,
                    )
                )
            except Exception:
                logger.debug("Failed to send typing indicator to %s", mask_pii(self._chat_id))

    async def _get_tool_permission(
        self,
        tool_obj: Tool,
        validated_args: dict[str, Any],
    ) -> tuple[PermissionLevel, str | None, str]:
        """Check the stored permission level for a tool (no prompting).

        Returns a tuple of ``(level, resource, description)`` where:
        - level is the resolved permission from the store or policy default
        - resource is the extracted resource key (for persistence), or None
        - description is a human-readable description of the tool action
        """
        policy = tool_obj.approval_policy
        if policy is None:
            return PermissionLevel.ALWAYS, None, tool_obj.name

        resource: str | None = None
        if policy.resource_extractor is not None:
            resource = policy.resource_extractor(validated_args)

        store = get_approval_store()
        level = await store.check_permission(
            self.user.id, tool_obj.name, resource=resource, default=policy.default_level
        )

        description = tool_obj.name
        if policy.description_builder is not None:
            description = policy.description_builder(validated_args)

        return level, resource, description

    def register_tools(self, tools: list[Tool]) -> None:
        """Register available tools for this agent session."""
        self.tools = tools
        self._tools_by_name = {}
        for tool in tools:
            if tool.name in self._tools_by_name:
                logger.warning("Duplicate tool name registered: %s", tool.name)
            self._tools_by_name[tool.name] = tool
        logger.debug(
            "Registered %d tools for user %s: %s",
            len(tools),
            self.user.id if self.user else "N/A",
            ", ".join(sorted(self._tools_by_name.keys())),
        )

    def _get_or_build_tool_schemas(self) -> list[dict[str, Any]] | None:
        """Return Anthropic tool schemas, rebuilding only when tools changed.

        The tool list is fixed at agent boot, so in the steady state this
        cache hits for every round after the first. The invalidation
        check (rebuild when the name sequence diverges from the cached
        prefix) stays as defense-in-depth against future regressions.

        Callers (``_call_llm_with_retry`` via ``apply_tool_caching``)
        may mutate the last entry to stamp a ``cache_control`` marker.
        That mutation is idempotent when re-applied to the already-stamped
        dict, so sharing the same list across rounds is safe and we
        intentionally do not copy on return.
        """
        if not self.tools:
            self._cached_tool_schemas = None
            return None
        cached = self._cached_tool_schemas
        current_names = [t.name for t in self.tools]
        if cached is not None and len(cached) == len(current_names):
            cached_names = [entry["name"] for entry in cached]
            if cached_names == current_names:
                return cached
        schemas = [tool_to_function_schema(t) for t in self.tools]
        self._cached_tool_schemas = schemas
        return schemas

    def _log_tool_prefix_stability(self, round_number: int) -> None:
        """Warn if the current tool-name sequence fails to preserve the prior
        prefix, which would bust the Anthropic tools prompt cache.

        The tool list is fixed at agent boot, so in the steady state the
        sequence is identical every round. Any reorder, removal, or
        mid-list insert would reset the cache for the tools block. Kept
        as defense-in-depth: emits a DEBUG line on growth and a WARNING
        when the prefix diverges.
        """
        current = [t.name for t in self.tools]
        prev = self._prev_tool_names
        if prev and current[: len(prev)] != prev:
            logger.warning(
                "Tool prefix changed non-monotonically at round %d, "
                "prompt cache for tools is likely invalidated. "
                "prev=%s current=%s",
                round_number,
                prev,
                current,
            )
        elif len(current) > len(prev):
            added = current[len(prev) :]
            logger.debug(
                "Tool list grew at round %d: added=%s (total=%d)",
                round_number,
                added,
                len(current),
            )
        self._prev_tool_names = current

    async def _build_system_prompt(self, message_context: str) -> tuple[str, str]:
        """Build the system prompt as ``(stable, dynamic)`` halves.

        *stable* goes in the cacheable ``system`` param; *dynamic* (memory,
        integrations) is appended to the current user turn so it does not
        invalidate the history cache (#1420).
        """
        return await build_agent_system_prompt_parts(
            self.user,
            self.tools,
            message_context,
        )

    async def _emit_response(
        self,
        response: MessageResponse,
        *,
        purpose: str,
        model: str,
        provider: str,
        started_at: datetime,
    ) -> None:
        """Build and dispatch an ``LLMResponsePayload`` for a single LLM round.

        Paired with the ``emit_llm_request`` call that fired just before
        the corresponding ``amessages`` call. Observers see the request
        first, then the response; ``request_id`` and ``started_at`` echo
        the request so consumers can pair them without depending on call
        ordering across concurrent agent loops.

        Content blocks are serialized to plain dicts so observers can
        store them as JSON without holding references to pydantic types
        from the any-llm provider package.
        """
        content_blocks: list[dict[str, Any]] = []
        for block in response.content:
            try:
                content_blocks.append(block.model_dump(mode="json"))
            except Exception:
                # Defensive: a provider returning an unexpected block shape
                # must not break the agent turn. Logged once per occurrence;
                # the observer just sees a partial content list.
                logger.exception(
                    "Failed to serialize response content block; skipping for observer"
                )
        usage = response.usage
        await emit_llm_response(
            LLMResponsePayload(
                schema_version=1,
                purpose=purpose,
                user_id=self.user.id,
                session_id=self._session_id or None,
                request_id=self._request_id or None,
                model=model,
                provider=provider,
                content_blocks=content_blocks,
                stop_reason=response.stop_reason,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cache_creation_input_tokens=(usage.cache_creation_input_tokens if usage else None),
                cache_read_input_tokens=(usage.cache_read_input_tokens if usage else None),
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
        )

    async def _call_llm_with_retry(
        self,
        messages: list[AgentMessage],
        tool_schemas: list[Any] | None,
        max_tokens: int | None = None,
    ) -> MessageResponse:
        """Call amessages with typed exception handling and retry logic.

        Accepts typed ``AgentMessage`` objects and serializes them to
        Anthropic Messages API format at the LLM boundary.  Handles
        RateLimitError (exponential backoff with jitter, up to
        ``LLM_MAX_RETRIES`` attempts) and ContextLengthExceededError
        (trim history and retry once).
        ContentFilterError and AuthenticationError are re-raised with
        appropriate logging so the caller can produce a user-facing message.
        """
        await self._send_typing_indicator()
        effective_max_tokens = max_tokens or settings.llm_max_tokens_agent
        system_str, msg_dicts = messages_to_messages_api(messages)
        msg_dicts = apply_history_cache_breakpoint(msg_dicts)
        # Rounds N > 0 end in tool results: mark the trailing block so the
        # current turn plus prior rounds read from cache instead of being
        # re-sent as fresh input every round (issue #1430). No-op on round 0.
        msg_dicts = apply_in_turn_cache_breakpoint(msg_dicts)
        system: str | list[dict[str, Any]] | None = system_str
        if system is not None:
            system = prepare_system_with_caching(system)
        if tool_schemas:
            tool_schemas = apply_tool_caching(tool_schemas)
        tool_count = len(tool_schemas) if tool_schemas else 0
        thinking = reasoning_effort_to_thinking(settings.reasoning_effort)
        effective_model = self._llm_model_override or settings.llm_model
        effective_provider = self._llm_provider_override or settings.llm_provider
        logger.debug(
            "Calling LLM: model=%s provider=%s messages=%d tools=%d max_tokens=%d",
            effective_model,
            effective_provider,
            len(msg_dicts),
            tool_count,
            effective_max_tokens,
        )
        started_at = datetime.now(UTC)
        await emit_llm_request(
            LLMRequestPayload(
                schema_version=1,
                purpose=PURPOSE_AGENT_MAIN,
                user_id=self.user.id,
                session_id=self._session_id or None,
                request_id=self._request_id or None,
                model=effective_model,
                provider=effective_provider,
                max_tokens=effective_max_tokens,
                thinking=thinking,
                system=system,
                messages=msg_dicts,
                tools=tool_schemas,
                min_message_seq_in_prompt=compute_min_message_seq(messages),
                started_at=started_at,
            )
        )
        for attempt in range(LLM_MAX_RETRIES):
            try:
                response = cast(
                    MessageResponse,
                    await amessages(
                        model=effective_model,
                        provider=effective_provider,
                        api_base=settings.llm_api_base,
                        system=system,
                        messages=msg_dicts,
                        tools=tool_schemas,
                        max_tokens=effective_max_tokens,
                        thinking=thinking,
                    ),
                )
                await self._emit_response(
                    response,
                    purpose=PURPOSE_AGENT_MAIN,
                    model=effective_model,
                    provider=effective_provider,
                    started_at=started_at,
                )
                return response
            except RateLimitError:
                if attempt == LLM_MAX_RETRIES - 1:
                    raise
                delay = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "Rate limited, retrying in %.1fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    LLM_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
            except ContextLengthExceededError:
                trim_result = trim_messages(
                    messages,
                    input_tokens=self._last_input_tokens or MAX_INPUT_TOKENS,
                )
                self._reactive_trim_dropped.extend(trim_result.dropped)
                logger.warning(
                    "Context length exceeded, trimmed from %d to %d messages and retrying",
                    len(messages),
                    len(trim_result.messages),
                )
                retry_system_str, trimmed_dicts = messages_to_messages_api(trim_result.messages)
                trimmed_dicts = apply_history_cache_breakpoint(trimmed_dicts)
                trimmed_dicts = apply_in_turn_cache_breakpoint(trimmed_dicts)
                system = (
                    prepare_system_with_caching(retry_system_str)
                    if retry_system_str is not None
                    else None
                )
                followup_started_at = datetime.now(UTC)
                await emit_llm_request(
                    LLMRequestPayload(
                        schema_version=1,
                        purpose=PURPOSE_AGENT_FOLLOWUP,
                        user_id=self.user.id,
                        session_id=self._session_id or None,
                        request_id=self._request_id or None,
                        model=effective_model,
                        provider=effective_provider,
                        max_tokens=effective_max_tokens,
                        thinking=thinking,
                        system=system,
                        messages=trimmed_dicts,
                        tools=tool_schemas,
                        min_message_seq_in_prompt=compute_min_message_seq(trim_result.messages),
                        started_at=followup_started_at,
                    )
                )
                followup_response = cast(
                    MessageResponse,
                    await amessages(
                        model=effective_model,
                        provider=effective_provider,
                        api_base=settings.llm_api_base,
                        system=system,
                        messages=trimmed_dicts,
                        tools=tool_schemas,
                        max_tokens=effective_max_tokens,
                        thinking=thinking,
                    ),
                )
                await self._emit_response(
                    followup_response,
                    purpose=PURPOSE_AGENT_FOLLOWUP,
                    model=effective_model,
                    provider=effective_provider,
                    started_at=followup_started_at,
                )
                return followup_response
            except ContentFilterError:
                logger.warning("Content blocked by provider safety filter")
                raise
            except AuthenticationError:
                logger.critical("LLM authentication failed -- check API key configuration")
                raise
        # This should be unreachable, but satisfies the type checker.
        raise RuntimeError("LLM retry loop exited without returning")

    def _validate_tool_args(
        self, tool: Tool, tool_args: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Validate tool arguments against the tool's params_model.

        Returns a tuple of (validated_args, error_message). When validation
        succeeds, error_message is None and validated_args contains the
        coerced values. When validation fails, error_message contains a
        structured description of the field errors.
        """
        try:
            validated = tool.params_model.model_validate(tool_args)
            return validated.model_dump(), None
        except ValidationError as exc:
            coerced_args = _stringify_numbers_for_string_fields(tool_args, exc)
            if coerced_args is not None:
                try:
                    validated = tool.params_model.model_validate(coerced_args)
                    return validated.model_dump(), None
                except ValidationError as retry_exc:
                    return tool_args, format_validation_error(tool.name, retry_exc, tool)
            return tool_args, format_validation_error(tool.name, exc, tool)

    def _get_tool_tags(self, tool_name: str) -> set[ToolTags]:
        """Look up the tags for a registered tool by name."""
        tool = self._tools_by_name.get(tool_name)
        return tool.tags if tool else set()

    async def _execute_single_tool(
        self,
        idx: int,
        tool_obj: Tool,
        validated_args: dict[str, Any],
        parsed_calls: list[ToolCallRequest],
    ) -> tuple[StoredToolInteraction, ToolResultMessage, str]:
        """Run one approved tool call and build its persisted record + result.

        Catches every exception and converts it into an error
        ``StoredToolInteraction`` so a single tool failure cannot bring
        down the rest of the parallel batch. Returns the record, the
        ``ToolResultMessage`` to append, and a human-readable action
        label for ``actions_taken``.
        """
        tc_req = parsed_calls[idx]
        tool_name = tc_req.name
        tool_tags = self._get_tool_tags(tool_name)

        # Telemetry: log specialist tool invocations with their category.
        if self._registry is not None:
            factory = self._registry.get_specialist_factory_for_tool(tool_name)
            if factory is not None:
                logger.info(
                    "specialist_tool_invocation tool=%s category=%s",
                    tool_name,
                    factory,
                )

        await self._emit(ToolExecutionStartEvent(tool_name=tool_name, arguments=validated_args))
        tool_start = time.monotonic()
        result_str = ""
        is_error = False
        action_label = ""
        record: StoredToolInteraction
        try:
            result = await tool_obj.function(**validated_args)
            result_str = result.content
            is_error = result.is_error
            if is_error:
                hint = build_error_hint(result)
                result_str += "\n\n" + hint
                action_label = f"Failed: {tool_name}"
            else:
                action_label = f"Called {tool_name}"
            stored_receipt = None
            if not is_error and result.receipt is not None:
                stored_receipt = StoredToolReceipt(
                    action=result.receipt.action,
                    target=result.receipt.target,
                    url=result.receipt.url,
                )
                # We do not echo the rendered receipt into result_str.
                # The earlier design appended a "the user already sees
                # this" preview hoping the LLM would skip restating it;
                # the LLM restated it anyway and ``append_receipts``
                # then added a second copy at dispatch, doubling every
                # receipt block in the outbound message. Migration 037
                # also closes the cross-turn version of this loop by
                # storing the LLM's pre-receipt prose in
                # ``messages.llm_reply_text`` so the history rebuilder
                # does not train the model on its own appended receipts.
            record = StoredToolInteraction(
                tool_call_id=tc_req.id,
                name=tool_name,
                args=validated_args,
                result=result_str,
                is_error=is_error,
                tags=set(tool_tags),
                receipt=stored_receipt,
            )
        except Exception as exc:
            logger.exception("Tool call failed: %s", tool_name)
            hint = _ERROR_KIND_HINTS[ToolErrorKind.INTERNAL]
            # Surface the real exception so the LLM can decide whether to
            # retry, adjust its args, or report the issue back to the user.
            # Scrub well-known secret formats first so a tool that wraps an
            # API key or OAuth token in its exception does not leak it into
            # the LLM context (and thus into provider logs and the messages
            # table). Cap the rendered string so a long traceback can not
            # blow the context budget.
            err_text = _scrub_secrets(f"{type(exc).__name__}: {exc}")
            result_str = f"Error: tool {tool_name} raised {err_text}\n\n{hint}"[:1500]
            is_error = True
            action_label = f"Failed: {tool_name}"
            record = StoredToolInteraction(
                tool_call_id=tc_req.id,
                name=tool_name,
                args=validated_args,
                result=result_str,
                is_error=True,
                tags=set(tool_tags),
                receipt=None,
            )
        tool_duration = (time.monotonic() - tool_start) * 1000
        logger.debug(
            "Tool %s completed in %.1fms, is_error=%s, result_length=%d",
            tool_name,
            tool_duration,
            is_error,
            len(result_str),
        )
        await self._emit(
            ToolExecutionEndEvent(
                tool_name=tool_name,
                result=result_str,
                is_error=is_error,
                duration_ms=tool_duration,
            )
        )
        tool_message = ToolResultMessage(
            tool_call_id=tc_req.id,
            content=result_str,
            is_error=is_error,
        )
        return record, tool_message, action_label

    async def _execute_tool_round(
        self,
        parsed_calls: list[ToolCallRequest],
        parsed_raw: list[ParsedToolCall],
        actions_taken: list[str],
        memories_saved: list[dict[str, str]],
        tool_call_records: list[StoredToolInteraction],
        response_truncated: bool = False,
    ) -> list[ToolResultMessage]:
        """Validate and execute a round of tool calls.

        Phase 1 validates all tool calls before executing any.
        Phase 2 runs approval checks and executes only the validated calls.
        Returns the list of ``ToolResultMessage`` objects for the round.
        """
        # -- Phase 1: validate ALL tool calls before executing any -------
        pre_validated: list[tuple[int, Tool, dict[str, Any]]] = []
        tool_results: list[ToolResultMessage] = []

        # Snapshot every (tool_name, normalized_args) pair the model has
        # already invoked in this turn (across prior rounds). Used to emit
        # a telemetry warning when the model fires the same call again,
        # which is how the CompanyCam upload-storm shows up in transcripts.
        # ``seen_in_round`` catches duplicates within this round's batch.
        seen_in_prior_rounds: set[tuple[str, str]] = {
            (rec.name, _normalize_tool_args(rec.args)) for rec in tool_call_records
        }
        seen_in_round: set[tuple[str, str]] = set()

        for i, tc_req in enumerate(parsed_calls):
            tool_name = tc_req.name
            tool_args = tc_req.arguments

            # Handle malformed arguments (arguments was None in ParsedToolCall)
            if not tool_args and parsed_raw[i].arguments is None:
                logger.warning(
                    "Malformed tool arguments for %s",
                    tool_name,
                )
                tool_results.append(
                    ToolResultMessage(
                        tool_call_id=tc_req.id,
                        content=f"Error: malformed arguments for {tool_name}",
                        is_error=True,
                    )
                )
                actions_taken.append(f"Failed: {tool_name} (bad args)")
                continue

            tool_obj = self._tools_by_name.get(tool_name)
            if not tool_obj:
                logger.debug("Unknown tool %r requested by LLM", tool_name)
                available = ", ".join(sorted(self._tools_by_name.keys()))
                result_str = (
                    f'Error: unknown tool "{tool_name}".'
                    f" Available tools: {available}"
                    f"\n\n{_DEFAULT_ERROR_HINT}"
                )
                tool_results.append(
                    ToolResultMessage(
                        tool_call_id=tc_req.id,
                        content=result_str,
                        is_error=True,
                    )
                )
                continue

            validated_args, validation_error = self._validate_tool_args(tool_obj, tool_args)
            if validation_error is not None:
                logger.warning(
                    "Validation failed for %s: %s",
                    tool_name,
                    validation_error,
                )
                tool_tags = self._get_tool_tags(tool_name)
                hint = (
                    _TRUNCATION_HINT
                    if response_truncated
                    else _ERROR_KIND_HINTS[ToolErrorKind.VALIDATION]
                )
                result_str = validation_error + "\n\n" + hint
                actions_taken.append(f"Failed: {tool_name} (validation)")
                tool_call_records.append(
                    StoredToolInteraction(
                        tool_call_id=tc_req.id,
                        name=tool_name,
                        args=tool_args,
                        result=result_str,
                        is_error=True,
                        tags=set(tool_tags),
                    )
                )
                tool_results.append(
                    ToolResultMessage(
                        tool_call_id=tc_req.id,
                        content=result_str,
                        is_error=True,
                    )
                )
                continue

            dup_key = (tool_name, _normalize_tool_args(validated_args))
            if dup_key in seen_in_prior_rounds or dup_key in seen_in_round:
                # Diagnostic only: do not block. The model legitimately
                # retries on transient errors, so an aggressive short-
                # circuit would mask real workflows. The structured
                # warning lets us measure the pattern across users.
                logger.warning(
                    "duplicate_tool_call_within_turn user=%s tool=%s args=%s",
                    self.user.id,
                    tool_name,
                    dup_key[1],
                )
            seen_in_round.add(dup_key)

            pre_validated.append((i, tool_obj, validated_args))

        # -- Phase 2: approve then execute ------------------------------------
        #
        # Partition validated tools by permission level. Tools that need
        # approval are prompted individually so the user can approve or
        # deny each one independently.

        auto_entries: list[_ToolEntry] = []
        ask_entries: list[tuple[_ToolEntry, str | None, str]] = []
        deny_entries: list[_ToolEntry] = []

        for entry in pre_validated:
            _i, tool_obj, v_args = entry
            level, resource, description = await self._get_tool_permission(tool_obj, v_args)
            if level == PermissionLevel.ALWAYS:
                auto_entries.append(entry)
            elif level == PermissionLevel.NEVER:
                # NEVER is the schema-level off switch: the registry
                # already filters NEVER tools out of the LLM schema in
                # router.py / heartbeat.py. Reaching this branch means
                # the resolved level differs from the schema decision
                # (e.g. a resource-scoped override flipped after the
                # schema was built). Surface it as a permission denial
                # rather than executing.
                deny_entries.append(entry)
            else:
                ask_entries.append((entry, resource, description))

        # Add error results for denied tools
        for i, _tool_obj, v_args in deny_entries:
            tc_req = parsed_calls[i]
            tool_tags = self._get_tool_tags(tc_req.name)
            hint = _ERROR_KIND_HINTS[ToolErrorKind.PERMISSION]
            deny_msg = f"Error: permission denied for tool '{tc_req.name}'\n\n{hint}"
            actions_taken.append(f"Denied: {tc_req.name}")
            tool_call_records.append(
                StoredToolInteraction(
                    tool_call_id=tc_req.id,
                    name=tc_req.name,
                    args=v_args,
                    result=deny_msg,
                    is_error=True,
                    tags=set(tool_tags),
                )
            )
            tool_results.append(
                ToolResultMessage(tool_call_id=tc_req.id, content=deny_msg, is_error=True)
            )

        # Determine which tools get executed
        approved_entries: list[_ToolEntry] = list(auto_entries)

        if ask_entries:
            store = get_approval_store()
            indexed_entries = list(enumerate(ask_entries))

            for pos, (entry, resource, description) in indexed_entries:
                idx, tool_obj, v_args = entry
                tc_req = parsed_calls[idx]

                cache_key = (tool_obj.name, resource)
                cached_decision = self._approval_cache.get(cache_key)
                if cached_decision is not None:
                    logger.debug(
                        "Reusing approval for %s (resource=%r) from this turn: %s",
                        tool_obj.name,
                        resource,
                        cached_decision,
                    )
                    decision = cached_decision
                elif self._publish_outbound is not None and self._chat_id is not None:
                    prompt = format_approval_message(tool_obj.name, description)

                    # We deliberately do not persist the approval prompt to the
                    # session here. Past attempts persisted it as an OUTBOUND
                    # message which then loaded back as an ``AssistantMessage``
                    # in the next turn's history. The LLM mimicked the format
                    # in subsequent prose replies, generating fake permission
                    # prompts without calling the actual tool. The user sees
                    # the prompt via the channel; the agent does not need it
                    # in conversation history.

                    if self._request_id:
                        from backend.app.bus import message_bus

                        await message_bus.publish_event(
                            self._request_id,
                            {"type": "approval_request", "content": prompt},
                        )

                    gate = get_approval_gate()
                    decision = await gate.request_approval(
                        user_id=self.user.id,
                        tool_name=tool_obj.name,
                        description=description,
                        publish_outbound=self._publish_outbound,
                        channel=self._channel,
                        chat_id=self._chat_id,
                        prompt=prompt,
                    )
                    # Cache every terminal decision so sibling calls with the
                    # same (tool, resource) in this round skip the prompt.
                    # INTERRUPTED is not terminal (user changed subject).
                    if decision != ApprovalDecision.INTERRUPTED:
                        self._approval_cache[cache_key] = decision
                else:
                    decision = ApprovalDecision.DENIED

                if decision in (ApprovalDecision.APPROVED, ApprovalDecision.ALWAYS_ALLOW):
                    approved_entries.append(entry)
                    if decision == ApprovalDecision.ALWAYS_ALLOW:
                        try:
                            await store.set_permission(
                                self.user.id, tool_obj.name, PermissionLevel.ALWAYS, resource
                            )
                        except Exception:
                            logger.warning("Failed to persist ALWAYS for tool %s", tool_obj.name)

                elif decision == ApprovalDecision.INTERRUPTED:
                    # User changed subject. Error this entry + all remaining.
                    for _p, (e, _r, d) in indexed_entries[pos:]:
                        i_rem, _t_rem, va_rem = e
                        tc_rem = parsed_calls[i_rem]
                        rem_tags = self._get_tool_tags(tc_rem.name)
                        hint = _ERROR_KIND_HINTS[ToolErrorKind.INTERRUPTED]
                        msg = (
                            f"Tool request interrupted: the user moved on to a "
                            f'different topic instead of approving "{d}". '
                            f"Do not proactively retry this tool; only call it "
                            f"again if the user explicitly asks.\n\n{hint}"
                        )
                        actions_taken.append(f"Interrupted: {tc_rem.name}")
                        tool_call_records.append(
                            StoredToolInteraction(
                                tool_call_id=tc_rem.id,
                                name=tc_rem.name,
                                args=va_rem,
                                result=msg,
                                is_error=True,
                                tags=set(rem_tags),
                            )
                        )
                        tool_results.append(
                            ToolResultMessage(tool_call_id=tc_rem.id, content=msg, is_error=True)
                        )
                    break

                else:  # DENIED / ALWAYS_DENY
                    if decision == ApprovalDecision.ALWAYS_DENY:
                        try:
                            await store.set_permission(
                                self.user.id, tool_obj.name, PermissionLevel.NEVER, resource
                            )
                        except Exception:
                            logger.warning("Failed to persist NEVER for tool %s", tool_obj.name)

                    tool_tags = self._get_tool_tags(tc_req.name)
                    hint = _ERROR_KIND_HINTS[ToolErrorKind.PERMISSION]
                    deny_msg = f"Error: permission denied for tool '{tc_req.name}'\n\n{hint}"
                    actions_taken.append(f"Denied: {tc_req.name}")
                    tool_call_records.append(
                        StoredToolInteraction(
                            tool_call_id=tc_req.id,
                            name=tc_req.name,
                            args=v_args,
                            result=deny_msg,
                            is_error=True,
                            tags=set(tool_tags),
                        )
                    )
                    tool_results.append(
                        ToolResultMessage(tool_call_id=tc_req.id, content=deny_msg, is_error=True)
                    )

        # Execute all approved tools.
        #
        # Tools from a single LLM turn run concurrently by default. Tools that
        # share a non-None ``concurrency_group`` serialize within that group
        # in submission order; different groups (and ungrouped tools) run in
        # parallel. The model is responsible for sequencing dependent calls
        # across turns; within a turn we only fan out what the model asked for.
        if approved_entries:
            await self._send_typing_indicator()

            schedule_units = _bucket_by_concurrency_group(approved_entries)

            async def _run_unit(
                unit: list[tuple[int, _ToolEntry]],
            ) -> list[tuple[int, StoredToolInteraction, ToolResultMessage, str]]:
                results: list[tuple[int, StoredToolInteraction, ToolResultMessage, str]] = []
                for pos, (idx, tool_obj_u, validated_args) in unit:
                    record, msg, label = await self._execute_single_tool(
                        idx, tool_obj_u, validated_args, parsed_calls
                    )
                    results.append((pos, record, msg, label))
                return results

            unit_outputs = await asyncio.gather(*(_run_unit(u) for u in schedule_units))

            results_by_pos: dict[int, tuple[StoredToolInteraction, ToolResultMessage, str]] = {}
            for unit_output in unit_outputs:
                for pos, record, msg, label in unit_output:
                    results_by_pos[pos] = (record, msg, label)

            # Append in original approved_entries order so persisted records
            # and tool_results match the sequence the model emitted.
            for pos in range(len(approved_entries)):
                record, msg, label = results_by_pos[pos]
                actions_taken.append(label)
                tool_call_records.append(record)
                tool_results.append(msg)

        return tool_results

    async def process_message(
        self,
        message_context: str,
        conversation_history: list[AgentMessage] | None = None,
        system_prompt_override: str | None = None,
        max_tokens: int | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop."""
        agent_start_time = time.monotonic()
        logger.debug(
            "Agent starting for user %s, message length=%d, history=%d messages",
            self.user.id,
            len(message_context),
            len(conversation_history) if conversation_history else 0,
        )
        # The system prompt splits into a stable half (cacheable, sent in
        # the ``system`` param) and a dynamic half (memory, integrations,
        # cross-session context). The dynamic half is appended to the
        # current user turn rather than the system param so a memory write
        # does not invalidate the message-history cache (#1420). An
        # override (e.g. onboarding) is treated as fully stable.
        if system_prompt_override is not None:
            stable_system, dynamic_context = system_prompt_override, ""
        else:
            stable_system, dynamic_context = await self._build_system_prompt(message_context)
        # The full assembled prompt for observers / debugging. The dynamic
        # half physically ships on the user turn now, but this field still
        # reflects everything the model was given as instruction context.
        system_prompt = (
            f"{stable_system}\n\n{dynamic_context}" if dynamic_context else stable_system
        )
        await self._emit(
            AgentStartEvent(
                user_id=self.user.id,
                message_context=message_context,
            )
        )

        messages: list[AgentMessage] = [SystemMessage(content=stable_system)]

        if conversation_history:
            messages.extend(conversation_history)

        time_context = build_time_user_context(self.user)
        # Order: time context, then dynamic context (memory, integrations,
        # cross-session), then the user's actual message last so the model
        # reads the ask after its context, mirroring how time is prepended.
        current_turn_parts = [time_context]
        if dynamic_context:
            current_turn_parts.append(dynamic_context)
        current_turn_parts.append(message_context)
        messages.append(UserMessage(content="\n\n".join(current_turn_parts)))

        # Trim oldest conversation history if content exceeds the limit.
        # Uses the block-based trimmer which preserves tool-call/result pairing
        # and injects a summary of dropped messages. The token budget is the
        # primary governor (fire at trigger, drop to target); the turn cap is
        # a backstop that catches message-count bloat even under the token
        # limit. Both use hysteresis to avoid re-firing compaction every turn.
        original_count = len(messages)
        # ``self._last_input_tokens`` is 0 on a fresh agent (one is built
        # per message), so fall back to the process-local per-user cache
        # of the last API-reported count. Only without either does the
        # trimmer use its chars/4 heuristic.
        trim_result = trim_messages(
            messages,
            target_tokens=settings.context_trim_target_tokens,
            target_turns=settings.context_trim_target_turns,
            trigger_tokens=settings.context_trim_trigger_tokens,
            input_tokens=self._last_input_tokens or _recall_input_tokens(self.user.id),
        )
        messages = trim_result.messages
        all_dropped = list(trim_result.dropped)
        trimmed_count = original_count - len(messages)
        if trimmed_count > 0:
            logger.warning(
                "Trimmed %d message(s) from conversation history (limit %d tokens, %d user turns)",
                trimmed_count,
                settings.context_trim_target_tokens,
                settings.context_trim_target_turns,
            )

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[StoredToolInteraction] = []
        reply_text = ""
        thinking_text = ""
        _total_input_tokens = 0
        _total_output_tokens = 0
        _total_cache_creation_tokens = 0
        _total_cache_read_tokens = 0

        for _round in range(MAX_TOOL_ROUNDS):
            logger.debug(
                "Round %d/%d starting, %d messages in context",
                _round,
                MAX_TOOL_ROUNDS,
                len(messages),
            )
            # Reuse cached tool schemas across rounds so identical rounds
            # don't re-serialize every Pydantic params model. The tool
            # list is fixed at agent boot, so this cache hits every round
            # after the first.
            tool_schemas = self._get_or_build_tool_schemas()
            self._log_tool_prefix_stability(_round)
            await self._emit(TurnStartEvent(round_number=_round, message_count=len(messages)))
            response = await self._call_llm_with_retry(
                messages, tool_schemas, max_tokens=max_tokens
            )
            purpose = "agent_main" if _round == 0 else "agent_followup"
            await log_llm_usage(
                self.user.id,
                self._llm_model_override or settings.llm_model,
                response,
                purpose,
                provider=self._llm_provider_override or settings.llm_provider,
            )
            if response.usage and response.usage.input_tokens:
                self._last_input_tokens = response.usage.input_tokens
                _remember_input_tokens(self.user.id, response.usage.input_tokens)
                _total_input_tokens += response.usage.input_tokens
                _total_output_tokens += response.usage.output_tokens or 0
                cache_create = response.usage.cache_creation_input_tokens or 0
                cache_read = response.usage.cache_read_input_tokens or 0
                _total_cache_creation_tokens += cache_create
                _total_cache_read_tokens += cache_read
                logger.debug(
                    "LLM usage: input_tokens=%d output_tokens=%d cache_create=%d cache_read=%d",
                    response.usage.input_tokens,
                    response.usage.output_tokens or 0,
                    cache_create,
                    cache_read,
                )

            # Guard: skip error responses to prevent context poisoning.
            # The user still sees the error fallback text, but the response
            # is NOT persisted to session history.
            if response.stop_reason not in _VALID_STOP_REASONS:
                logger.warning(
                    "Round %d: LLM returned error stop_reason=%r, aborting loop",
                    _round,
                    response.stop_reason,
                )
                # Compact any messages already dropped before early return
                if self._reactive_trim_dropped:
                    all_dropped.extend(self._reactive_trim_dropped)
                    self._reactive_trim_dropped = []
                if all_dropped:
                    from backend.app.agent.context import trigger_compaction_for_dropped

                    await trigger_compaction_for_dropped(self.user.id, all_dropped)

                total_duration = (time.monotonic() - agent_start_time) * 1000
                await self._emit(
                    AgentEndEvent(
                        reply_text=_LLM_ERROR_FALLBACK,
                        actions_taken=actions_taken,
                        total_duration_ms=total_duration,
                    )
                )
                return AgentResponse(
                    reply_text=_LLM_ERROR_FALLBACK,
                    actions_taken=actions_taken,
                    memories_saved=memories_saved,
                    tool_calls=tool_call_records,
                    is_error_fallback=True,
                    total_input_tokens=_total_input_tokens,
                    total_output_tokens=_total_output_tokens,
                    total_cache_creation_input_tokens=_total_cache_creation_tokens,
                    total_cache_read_input_tokens=_total_cache_read_tokens,
                    system_prompt=system_prompt,
                    # Surface any reasoning that preceded the error stop so
                    # downstream observers (and a future persistence policy
                    # that records error fallbacks) can see what the model
                    # was working through before it bailed. Today
                    # ``persist_outbound`` short-circuits on
                    # ``is_error_fallback``, so this rides along the in-memory
                    # response only.
                    thinking_text=get_response_thinking(response),
                )

            # Parse tool calls via shared parser
            parsed_raw = parse_tool_calls(response)
            if not parsed_raw:
                reply_text = get_response_text(response)
                # Capture thinking from the final response only. Earlier
                # rounds produced tool calls and their thinking justifies
                # a tool decision rather than the user-visible reply, so
                # we keep the persisted record aligned with the message
                # body the user actually saw.
                thinking_text = get_response_thinking(response)

                # Empty reply after tools is intentional silent action; do not re-prompt.
                if not reply_text and actions_taken:
                    logger.debug(
                        "Round %d: empty reply after %d tool call(s); treating as silent action",
                        _round,
                        len(actions_taken),
                    )

                logger.debug(
                    "Round %d: no tool calls, final reply length=%d",
                    _round,
                    len(reply_text),
                )
                await self._emit(TurnEndEvent(round_number=_round, has_more_tool_calls=False))
                break

            # Convert to typed ToolCallRequest objects
            parsed_calls: list[ToolCallRequest] = []
            for ptc in parsed_raw:
                parsed_calls.append(
                    ToolCallRequest(
                        id=ptc.id,
                        name=ptc.name,
                        arguments=ptc.arguments if ptc.arguments is not None else {},
                    )
                )
            logger.debug(
                "Round %d: LLM requested %d tool call(s): %s",
                _round,
                len(parsed_calls),
                ", ".join(tc.name for tc in parsed_calls),
            )

            # Append the assistant message (with tool_calls) to conversation
            messages.append(
                AssistantMessage(
                    content=get_response_text(response) or None,
                    tool_calls=parsed_calls,
                )
            )

            # Detect truncated responses: when the LLM hits max_tokens while
            # generating a tool call, the JSON payload may be incomplete.
            response_truncated = response.stop_reason == "max_tokens"

            # Execute the tool round (validate, approve, run)
            tool_results = await self._execute_tool_round(
                parsed_calls,
                parsed_raw,
                actions_taken,
                memories_saved,
                tool_call_records,
                response_truncated=response_truncated,
            )

            # If the response was truncated and produced validation errors,
            # auto-increase max_tokens for the next round so the LLM has
            # enough room to generate the full tool call payload. The
            # 8192 cap leaves one further recovery step beyond the
            # current 2048 default (2048 -> 4096 -> 8192) before we
            # give up and surface the truncation to the user.
            if response_truncated and any(r.is_error for r in tool_results):
                effective = max_tokens or settings.llm_max_tokens_agent
                max_tokens = min(effective * 2, 8192)
                logger.info(
                    "Response truncated with errors, increasing max_tokens to %d",
                    max_tokens,
                )

            messages.extend(tool_results)
            await self._emit(TurnEndEvent(round_number=_round, has_more_tool_calls=True))
        else:
            # Max rounds reached -- use last response content
            reply_text = get_response_text(response)
            thinking_text = get_response_thinking(response)
            logger.debug("Max tool rounds (%d) reached, using last response", MAX_TOOL_ROUNDS)

        # Collect any messages dropped by reactive trimming (ContextLengthExceededError)
        if self._reactive_trim_dropped:
            all_dropped.extend(self._reactive_trim_dropped)
            self._reactive_trim_dropped = []

        # Trigger background compaction for all dropped messages
        if all_dropped:
            from backend.app.agent.context import trigger_compaction_for_dropped

            await trigger_compaction_for_dropped(self.user.id, all_dropped)

        total_duration = (time.monotonic() - agent_start_time) * 1000
        logger.debug(
            "Agent finished for user %s in %.1fms, actions=%s, reply_length=%d",
            self.user.id,
            total_duration,
            actions_taken or "(none)",
            len(reply_text),
        )
        _cacheable_total = _total_cache_creation_tokens + _total_cache_read_tokens
        _cache_hit_ratio = _total_cache_read_tokens / _cacheable_total if _cacheable_total else 0.0
        logger.info(
            "Agent turn cache summary: user=%s input=%d output=%d "
            "cache_create=%d cache_read=%d hit_ratio=%.2f",
            self.user.id,
            _total_input_tokens,
            _total_output_tokens,
            _total_cache_creation_tokens,
            _total_cache_read_tokens,
            _cache_hit_ratio,
        )
        await self._emit(
            AgentEndEvent(
                reply_text=reply_text,
                actions_taken=actions_taken,
                total_duration_ms=total_duration,
            )
        )

        return AgentResponse(
            reply_text=reply_text,
            actions_taken=actions_taken,
            memories_saved=memories_saved,
            tool_calls=tool_call_records,
            total_input_tokens=_total_input_tokens,
            total_output_tokens=_total_output_tokens,
            total_cache_creation_input_tokens=_total_cache_creation_tokens,
            total_cache_read_input_tokens=_total_cache_read_tokens,
            system_prompt=system_prompt,
            thinking_text=thinking_text,
        )

    def _find_tool(self, name: str) -> Callable[..., Any] | None:
        """Find a registered tool by name."""
        tool = self._tools_by_name.get(name)
        return tool.function if tool else None
