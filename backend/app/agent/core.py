import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from any_llm import (
    AuthenticationError,
    ContentFilterError,
    ContextLengthExceededError,
    RateLimitError,
    acompletion,
)
from any_llm.types.completion import ChatCompletion
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from backend.app.agent.llm_parsing import parse_tool_calls
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
    messages_to_dicts,
)
from backend.app.agent.system_prompt import build_agent_system_prompt
from backend.app.agent.tools.base import (
    Tool,
    ToolErrorKind,
    ToolResult,
    ToolTags,
    tool_to_openai_schema,
)
from backend.app.config import settings
from backend.app.models import Contractor

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
RATE_LIMIT_RETRY_DELAY = 2.0
# Target token budget when trimming for context length (leave room for output tokens)
CONTEXT_TRIM_TARGET_TOKENS = 80_000

# Conservative default; most models support 128K+ but we leave room for output
MAX_INPUT_TOKENS = 120_000

# Per-message overhead tokens for role/delimiters/structural framing
_MESSAGE_OVERHEAD_TOKENS = 4
# Characters per token ratio for English text (slightly more accurate than 4.0)
_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(messages: list[AgentMessage]) -> int:
    """Estimate token count from typed messages, including tool call content.

    Counts content and any tool-call function names + serialized arguments.
    Adds a small per-message overhead for role and delimiter tokens.
    """
    total = 0
    for m in messages:
        total += _MESSAGE_OVERHEAD_TOKENS

        if isinstance(m, (SystemMessage, UserMessage)):
            if m.content:
                total += int(len(m.content) / _CHARS_PER_TOKEN)
        elif isinstance(m, AssistantMessage):
            if m.content:
                total += int(len(m.content) / _CHARS_PER_TOKEN)
            for tc in m.tool_calls:
                total += int(len(tc.name) / _CHARS_PER_TOKEN)
                # Estimate from the dict representation of arguments
                args_str = str(tc.arguments)
                total += int(len(args_str) / _CHARS_PER_TOKEN)
        elif isinstance(m, ToolResultMessage) and m.content:
            total += int(len(m.content) / _CHARS_PER_TOKEN)

    return total


def _format_validation_error(tool_name: str, exc: ValidationError, tool: Tool | None = None) -> str:
    """Format a Pydantic ValidationError into a structured message for the LLM."""
    error_lines: list[str] = [f"Validation error for {tool_name}:"]
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err["loc"])
        error_lines.append(f"  {loc}: {err['msg']} (type={err['type']})")

    if tool is not None:
        schema_summary = _summarize_tool_params(tool)
        if schema_summary:
            error_lines.append(f"\nExpected parameters: {schema_summary}")

    return "\n".join(error_lines)


def _summarize_tool_params(tool: Tool) -> str:
    """Build a concise parameter summary string from a tool's schema."""
    if tool.params_model is not None:
        schema = tool.params_model.model_json_schema()
    elif tool.parameters:
        schema = tool.parameters
    else:
        return ""

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return ""

    parts: list[str] = []
    for name, info in props.items():
        ptype = info.get("type", "any")
        req = "required" if name in required else "optional"
        default = info.get("default")
        if default is not None:
            parts.append(f'"{name}": {ptype} ({req}, default: {default})')
        else:
            parts.append(f'"{name}": {ptype} ({req})')
    return "{" + ", ".join(parts) + "}"


_DEFAULT_ERROR_HINT = "[Analyze the error above and try a different approach.]"

_ERROR_KIND_HINTS: dict[ToolErrorKind, str] = {
    ToolErrorKind.VALIDATION: (
        "[Check the expected parameter format and try again with corrected arguments.]"
    ),
    ToolErrorKind.NOT_FOUND: (
        "[The requested resource was not found. Verify the identifier and try again.]"
    ),
    ToolErrorKind.SERVICE: (
        "[An external service is temporarily unavailable."
        " Try a different approach or inform the user.]"
    ),
    ToolErrorKind.PERMISSION: ("[You do not have permission for this operation. Inform the user.]"),
    ToolErrorKind.INTERNAL: (
        "[An internal error occurred."
        " Inform the user that this operation is temporarily unavailable.]"
    ),
}


def _build_error_hint(result: ToolResult) -> str:
    """Build the LLM guidance suffix for an error ToolResult.

    Priority: explicit ``hint`` on the result, then ``error_kind`` mapping,
    then the generic default.
    """
    if result.hint:
        return f"[{result.hint}]" if not result.hint.startswith("[") else result.hint
    if result.error_kind is not None:
        return _ERROR_KIND_HINTS.get(result.error_kind, _DEFAULT_ERROR_HINT)
    return _DEFAULT_ERROR_HINT


@dataclass
class AgentResponse:
    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    memories_saved: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    is_error_fallback: bool = False


class BackshopAgent:
    """Main agent that processes contractor messages and produces actions."""

    def __init__(self, db: Session, contractor: Contractor) -> None:
        self.db = db
        self.contractor = contractor
        self.tools: list[Tool] = []
        self._tools_by_name: dict[str, Tool] = {}
        self._subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = []

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

    def register_tools(self, tools: list[Tool]) -> None:
        """Register available tools for this agent session."""
        self.tools = tools
        self._tools_by_name = {}
        for tool in tools:
            if tool.name in self._tools_by_name:
                logger.warning("Duplicate tool name registered: %s", tool.name)
            self._tools_by_name[tool.name] = tool

    async def _build_system_prompt(self, message_context: str) -> str:
        """Build the full system prompt via the composable builder."""
        return await build_agent_system_prompt(
            self.db, self.contractor, self.tools, message_context
        )

    async def _call_llm_with_retry(
        self,
        messages: list[AgentMessage],
        tool_schemas: list[Any] | None,
        llm_kwargs: dict[str, Any],
    ) -> ChatCompletion:
        """Call acompletion with typed exception handling and retry logic.

        Accepts typed ``AgentMessage`` objects and serializes them to dicts
        at the LLM API boundary.  Handles RateLimitError (retry once after
        delay) and ContextLengthExceededError (trim history and retry once).
        ContentFilterError and AuthenticationError are re-raised with
        appropriate logging so the caller can produce a user-facing message.
        """
        msg_dicts = messages_to_dicts(messages)
        try:
            return cast(
                ChatCompletion,
                await acompletion(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    messages=msg_dicts,  # type: ignore[arg-type]
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens_agent,
                    **llm_kwargs,
                ),
            )
        except RateLimitError:
            logger.warning("Rate limit hit, retrying after %.1fs delay", RATE_LIMIT_RETRY_DELAY)
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            return cast(
                ChatCompletion,
                await acompletion(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    messages=msg_dicts,  # type: ignore[arg-type]
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens_agent,
                    **llm_kwargs,
                ),
            )
        except ContextLengthExceededError:
            trimmed = self._trim_messages(messages)
            logger.warning(
                "Context length exceeded, trimmed from %d to %d messages and retrying",
                len(messages),
                len(trimmed),
            )
            trimmed_dicts = messages_to_dicts(trimmed)
            return cast(
                ChatCompletion,
                await acompletion(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    messages=trimmed_dicts,  # type: ignore[arg-type]
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens_agent,
                    **llm_kwargs,
                ),
            )
        except ContentFilterError:
            logger.warning("Content blocked by provider safety filter")
            raise
        except AuthenticationError:
            logger.critical("LLM authentication failed -- check API key configuration")
            raise

    @staticmethod
    def _trim_messages(
        messages: list[AgentMessage],
        target_tokens: int = CONTEXT_TRIM_TARGET_TOKENS,
    ) -> list[AgentMessage]:
        """Trim conversation messages to fit within a token budget.

        Keeps the system prompt (first message) and removes the oldest
        conversation messages until the estimated token count is at or below
        *target_tokens*. Tool-call / tool-result pairs are treated as atomic
        units: an ``AssistantMessage`` with ``tool_calls`` is never removed
        without also removing the ``ToolResultMessage`` entries that follow it
        (and vice-versa).
        """
        if len(messages) <= 2:
            return messages

        if _estimate_tokens(messages) <= target_tokens:
            return messages

        system = messages[0]
        body = list(messages[1:])

        # Group the body into "blocks" that must be removed together.
        blocks: list[list[AgentMessage]] = []
        i = 0
        while i < len(body):
            msg = body[i]
            if isinstance(msg, AssistantMessage) and msg.tool_calls:
                block: list[AgentMessage] = [msg]
                j = i + 1
                while j < len(body):
                    if isinstance(body[j], ToolResultMessage):
                        block.append(body[j])
                        j += 1
                    else:
                        break
                blocks.append(block)
                i = j
            else:
                blocks.append([msg])
                i += 1

        # Remove blocks from the front (oldest) until we fit the budget,
        # but always keep at least the last block.
        while len(blocks) > 1:
            remaining: list[AgentMessage] = [system]
            for blk in blocks:
                remaining.extend(blk)
            if _estimate_tokens(remaining) <= target_tokens:
                break
            blocks.pop(0)

        result: list[AgentMessage] = [system]
        for blk in blocks:
            result.extend(blk)
        return result

    def _validate_tool_args(
        self, tool: Tool, tool_args: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Validate tool arguments against the tool's params_model if present.

        Returns a tuple of (validated_args, error_message). When validation
        succeeds, error_message is None and validated_args contains the
        coerced values. When validation fails, error_message contains a
        structured description of the field errors.
        """
        if tool.params_model is None:
            return tool_args, None

        try:
            validated = tool.params_model.model_validate(tool_args)
            return validated.model_dump(), None
        except ValidationError as exc:
            return tool_args, _format_validation_error(tool.name, exc, tool)

    def _get_tool_tags(self, tool_name: str) -> set[str]:
        """Look up the tags for a registered tool by name."""
        tool = self._tools_by_name.get(tool_name)
        return tool.tags if tool else set()

    async def process_message(
        self,
        message_context: str,
        conversation_history: list[AgentMessage] | list[dict[str, str]] | None = None,
        system_prompt_override: str | None = None,
        temperature: float | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop.

        *conversation_history* accepts both typed ``AgentMessage`` objects
        (preferred) and legacy ``dict`` messages for backward compatibility.
        """
        agent_start_time = time.monotonic()
        system_prompt = system_prompt_override or await self._build_system_prompt(message_context)
        await self._emit(
            AgentStartEvent(
                contractor_id=self.contractor.id,
                message_context=message_context,
            )
        )

        messages: list[AgentMessage] = [SystemMessage(content=system_prompt)]

        if conversation_history:
            for entry in conversation_history:
                if isinstance(entry, dict):
                    messages.append(_dict_to_message(cast(dict[str, Any], entry)))
                else:
                    messages.append(entry)

        messages.append(UserMessage(content=message_context))

        # Trim oldest conversation history if estimated tokens exceed the limit
        original_count = len(messages)
        estimated = _estimate_tokens(messages)
        while estimated > MAX_INPUT_TOKENS and len(messages) > 2:
            messages.pop(1)
            estimated = _estimate_tokens(messages)
        trimmed_count = original_count - len(messages)
        if trimmed_count > 0:
            logger.warning(
                "Trimmed %d message(s) from conversation history to fit context window "
                "(estimated %d tokens, limit %d)",
                trimmed_count,
                _estimate_tokens(messages),
                MAX_INPUT_TOKENS,
            )

        tool_schemas = [tool_to_openai_schema(t) for t in self.tools] if self.tools else None

        llm_kwargs: dict[str, Any] = {}
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[dict[str, Any]] = []
        reply_text = ""

        for _round in range(MAX_TOOL_ROUNDS):
            await self._emit(TurnStartEvent(round_number=_round, message_count=len(messages)))
            response = await self._call_llm_with_retry(messages, tool_schemas, llm_kwargs)

            # Parse tool calls via shared parser
            parsed_raw = parse_tool_calls(response)
            if not parsed_raw:
                reply_text = response.choices[0].message.content or ""
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

            # Append the assistant message (with tool_calls) to conversation
            messages.append(
                AssistantMessage(
                    content=response.choices[0].message.content,
                    tool_calls=parsed_calls,
                )
            )

            tool_results: list[ToolResultMessage] = []
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
                        )
                    )
                    actions_taken.append(f"Failed: {tool_name} (bad args)")
                    continue

                tool_obj = self._tools_by_name.get(tool_name)
                tool_func = tool_obj.function if tool_obj else None
                tool_tags = self._get_tool_tags(tool_name)
                result_str = ""
                is_error = False
                if tool_func and tool_obj:
                    validated_args, validation_error = self._validate_tool_args(tool_obj, tool_args)
                    if validation_error is not None:
                        logger.warning(
                            "Validation failed for %s: %s",
                            tool_name,
                            validation_error,
                        )
                        hint = _ERROR_KIND_HINTS[ToolErrorKind.VALIDATION]
                        result_str = validation_error + "\n\n" + hint
                        is_error = True
                        actions_taken.append(f"Failed: {tool_name} (validation)")
                        tool_call_records.append(
                            {
                                "name": tool_name,
                                "args": tool_args,
                                "result": result_str,
                                "is_error": True,
                                "tags": tool_tags,
                            }
                        )
                        tool_results.append(
                            ToolResultMessage(
                                tool_call_id=tc_req.id,
                                content=result_str,
                            )
                        )
                        continue

                    await self._emit(
                        ToolExecutionStartEvent(tool_name=tool_name, arguments=validated_args)
                    )
                    tool_start = time.monotonic()
                    try:
                        result = await tool_func(**validated_args)
                        if not isinstance(result, ToolResult):
                            logger.warning(
                                "Tool %s returned %s instead of ToolResult",
                                tool_name,
                                type(result).__name__,
                            )
                            result = ToolResult(content=str(result))
                        result_str = result.content
                        is_error = result.is_error
                        if is_error:
                            hint = _build_error_hint(result)
                            result_str += "\n\n" + hint
                        if is_error:
                            actions_taken.append(f"Failed: {tool_name}")
                        else:
                            actions_taken.append(f"Called {tool_name}")
                        tool_call_records.append(
                            {
                                "name": tool_name,
                                "args": validated_args,
                                "result": result_str,
                                "is_error": is_error,
                                "tags": tool_tags,
                            }
                        )
                        if ToolTags.SAVES_MEMORY in tool_tags:
                            memories_saved.append(validated_args)
                    except Exception:
                        logger.exception("Tool call failed: %s", tool_name)
                        hint = _ERROR_KIND_HINTS[ToolErrorKind.INTERNAL]
                        result_str = f"Error: tool {tool_name} failed\n\n{hint}"
                        is_error = True
                        actions_taken.append(f"Failed: {tool_name}")
                    tool_duration = (time.monotonic() - tool_start) * 1000
                    await self._emit(
                        ToolExecutionEndEvent(
                            tool_name=tool_name,
                            result=result_str,
                            is_error=is_error,
                            duration_ms=tool_duration,
                        )
                    )
                else:
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
                    )
                )

            messages.extend(tool_results)
            await self._emit(TurnEndEvent(round_number=_round, has_more_tool_calls=True))
        else:
            # Max rounds reached -- use last response content
            reply_text = response.choices[0].message.content or ""

        total_duration = (time.monotonic() - agent_start_time) * 1000
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
        )

    def _find_tool(self, name: str) -> Callable[..., Any] | None:
        """Find a registered tool by name."""
        tool = self._tools_by_name.get(name)
        return tool.function if tool else None


def _dict_to_message(d: dict[str, Any]) -> AgentMessage:
    """Convert a legacy dict message to a typed message object."""
    role = d.get("role", "user")
    content = d.get("content", "")
    if role == "system":
        return SystemMessage(content=content)
    if role == "assistant":
        return AssistantMessage(content=content)
    if role == "tool":
        return ToolResultMessage(
            tool_call_id=d.get("tool_call_id", ""),
            content=content,
        )
    return UserMessage(content=content)
