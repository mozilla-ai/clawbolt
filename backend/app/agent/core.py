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
    amessages,
)
from any_llm.types.messages import MessageResponse
from pydantic import ValidationError

from backend.app.agent.approval import (
    ApprovalDecision,
    PermissionLevel,
    get_approval_gate,
    get_approval_store,
)
from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from backend.app.agent.file_store import UserData
from backend.app.agent.llm_parsing import ParsedToolCall, get_response_text, parse_tool_calls
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
    messages_to_messages_api,
)
from backend.app.agent.system_prompt import build_agent_system_prompt
from backend.app.agent.tool_errors import (
    _DEFAULT_ERROR_HINT,
    _ERROR_KIND_HINTS,
    build_error_hint,
    format_validation_error,
)
from backend.app.agent.tools.base import (
    Tool,
    ToolErrorKind,
    ToolTags,
    tool_to_function_schema,
)
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext, ToolRegistry
from backend.app.agent.trimming import trim_messages
from backend.app.config import settings
from backend.app.services.llm_usage import log_llm_usage

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = settings.max_tool_rounds
RATE_LIMIT_RETRY_DELAY = settings.rate_limit_retry_delay

# Conservative default; most models support 128K+ but we leave room for output
MAX_INPUT_TOKENS = settings.max_input_tokens

# Stop reasons that represent a valid, non-error LLM response.
# Anything outside this set indicates a provider-level error and the
# response should *not* be persisted to session history to avoid
# context poisoning.
_VALID_STOP_REASONS: set[str | None] = {"end_turn", "max_tokens", "tool_use", "stop_sequence", None}

_LLM_ERROR_FALLBACK = "I'm having trouble thinking right now. Can you try again in a moment?"


@dataclass
class AgentResponse:
    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    memories_saved: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[StoredToolInteraction] = field(default_factory=list)
    is_error_fallback: bool = False


class ClawboltAgent:
    """Main agent that processes user messages and produces actions."""

    def __init__(
        self,
        user: UserData,
        channel: str = "",
        publish_outbound: Callable[[Any], Awaitable[None]] | None = None,
        chat_id: str | None = None,
        tool_context: ToolContext | None = None,
        registry: ToolRegistry | None = None,
        session_id: str = "",
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
        self._activated_specialists: set[str] = set()
        self._last_input_tokens: int = 0
        self._session_id = session_id

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
                logger.debug("Failed to send typing indicator to %s", self._chat_id)

    async def _check_approval(
        self,
        tool_obj: Tool,
        validated_args: dict[str, Any],
    ) -> PermissionLevel:
        """Check approval for a tool call.

        Returns ``PermissionLevel.AUTO`` when execution should proceed,
        or ``PermissionLevel.DENY`` when it should be blocked.
        """
        policy = tool_obj.approval_policy
        if policy is None:
            return PermissionLevel.AUTO

        resource: str | None = None
        if policy.resource_extractor is not None:
            resource = policy.resource_extractor(validated_args)

        store = get_approval_store()
        level = store.check_permission(
            self.user.id, tool_obj.name, resource=resource, default=policy.default_level
        )

        if level == PermissionLevel.AUTO:
            return PermissionLevel.AUTO
        if level == PermissionLevel.DENY:
            return PermissionLevel.DENY

        # ASK: prompt the user.  When no outbound channel is available
        # (e.g. headless/API usage, dashboard without WebSocket reply path)
        # we fall through to AUTO so the agent is not permanently blocked.
        if self._publish_outbound is None or self._chat_id is None:
            return PermissionLevel.AUTO

        gate = get_approval_gate()

        # Only one approval prompt per user at a time.  If a prior tool in
        # the same round is already waiting, deny this one to avoid
        # overwriting the pending request.
        if gate.has_pending(self.user.id):
            return PermissionLevel.DENY

        description = tool_obj.name
        if policy.description_builder is not None:
            description = policy.description_builder(validated_args)

        decision = await gate.request_approval(
            user_id=self.user.id,
            tool_name=tool_obj.name,
            description=description,
            publish_outbound=self._publish_outbound,
            channel=self._channel,
            chat_id=self._chat_id,
        )

        if decision == ApprovalDecision.ALWAYS_ALLOW:
            store.set_permission(self.user.id, tool_obj.name, PermissionLevel.AUTO, resource)
            return PermissionLevel.AUTO
        if decision == ApprovalDecision.APPROVED:
            return PermissionLevel.AUTO
        if decision == ApprovalDecision.ALWAYS_DENY:
            store.set_permission(self.user.id, tool_obj.name, PermissionLevel.DENY, resource)
            return PermissionLevel.DENY
        # DENIED or timeout
        return PermissionLevel.DENY

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

    def _activate_specialist(self, factory_name: str) -> None:
        """Activate a specialist tool factory, injecting its tools for the next round.

        Only marks the factory as activated if at least one tool was
        actually created (dependencies like storage may prevent creation).
        """
        if factory_name in self._activated_specialists:
            return
        if self._registry is None or self._tool_context is None:
            return
        new_tools = self._registry.create_tools(
            self._tool_context,
            selected_factories={factory_name},
        )
        if not new_tools:
            logger.debug(
                "Specialist factory %r produced no tools (dependencies unmet?)", factory_name
            )
            return
        self._activated_specialists.add(factory_name)
        new_names: list[str] = []
        for tool in new_tools:
            if tool.name not in self._tools_by_name:
                self.tools.append(tool)
                self._tools_by_name[tool.name] = tool
                new_names.append(tool.name)
        logger.debug(
            "Activated specialist %r, added tools: %s",
            factory_name,
            ", ".join(new_names) or "(none new)",
        )

    def _check_specialist_activations(
        self,
        parsed_calls: list[ToolCallRequest],
    ) -> bool:
        """Check for list_capabilities calls and activate requested specialists.

        Returns True if any new specialist factories were activated (meaning
        tool schemas need to be rebuilt for the next round).
        """
        if self._registry is None:
            return False
        activated_any = False
        specialist_names = self._registry.specialist_factory_names
        for tc_req in parsed_calls:
            if tc_req.name != ToolName.LIST_CAPABILITIES:
                continue
            category = tc_req.arguments.get("category")
            if (
                category
                and category in specialist_names
                and category not in self._activated_specialists
            ):
                self._activate_specialist(category)
                activated_any = True
        return activated_any

    async def _build_system_prompt(self, message_context: str) -> str:
        """Build the full system prompt via the composable builder."""
        return await build_agent_system_prompt(
            self.user,
            self.tools,
            message_context,
            current_session_id=self._session_id,
        )

    async def _call_llm_with_retry(
        self,
        messages: list[AgentMessage],
        tool_schemas: list[Any] | None,
        llm_kwargs: dict[str, Any],
    ) -> MessageResponse:
        """Call amessages with typed exception handling and retry logic.

        Accepts typed ``AgentMessage`` objects and serializes them to
        Anthropic Messages API format at the LLM boundary.  Handles
        RateLimitError (retry once after delay) and
        ContextLengthExceededError (trim history and retry once).
        ContentFilterError and AuthenticationError are re-raised with
        appropriate logging so the caller can produce a user-facing message.
        """
        await self._send_typing_indicator()
        system, msg_dicts = messages_to_messages_api(messages)
        tool_count = len(tool_schemas) if tool_schemas else 0
        logger.debug(
            "Calling LLM: model=%s provider=%s messages=%d tools=%d max_tokens=%d",
            settings.llm_model,
            settings.llm_provider,
            len(msg_dicts),
            tool_count,
            settings.llm_max_tokens_agent,
        )
        try:
            return cast(
                MessageResponse,
                await amessages(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    system=system,
                    messages=msg_dicts,
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens_agent,
                    **llm_kwargs,
                ),
            )
        except RateLimitError:
            logger.warning("Rate limit hit, retrying after %.1fs delay", RATE_LIMIT_RETRY_DELAY)
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            return cast(
                MessageResponse,
                await amessages(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    system=system,
                    messages=msg_dicts,
                    tools=tool_schemas,
                    max_tokens=settings.llm_max_tokens_agent,
                    **llm_kwargs,
                ),
            )
        except ContextLengthExceededError:
            trimmed = trim_messages(
                messages,
                input_tokens=self._last_input_tokens or MAX_INPUT_TOKENS,
            )
            logger.warning(
                "Context length exceeded, trimmed from %d to %d messages and retrying",
                len(messages),
                len(trimmed),
            )
            system, trimmed_dicts = messages_to_messages_api(trimmed)
            return cast(
                MessageResponse,
                await amessages(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    system=system,
                    messages=trimmed_dicts,
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
            return tool_args, format_validation_error(tool.name, exc, tool)

    def _get_tool_tags(self, tool_name: str) -> set[ToolTags]:
        """Look up the tags for a registered tool by name."""
        tool = self._tools_by_name.get(tool_name)
        return tool.tags if tool else set()

    async def _execute_tool_round(
        self,
        parsed_calls: list[ToolCallRequest],
        parsed_raw: list[ParsedToolCall],
        actions_taken: list[str],
        memories_saved: list[dict[str, str]],
        tool_call_records: list[StoredToolInteraction],
    ) -> list[ToolResultMessage]:
        """Validate and execute a round of tool calls.

        Phase 1 validates all tool calls before executing any.
        Phase 2 runs approval checks and executes only the validated calls.
        Returns the list of ``ToolResultMessage`` objects for the round.
        """
        # -- Phase 1: validate ALL tool calls before executing any -------
        pre_validated: list[tuple[int, Tool, dict[str, Any]]] = []
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
                hint = _ERROR_KIND_HINTS[ToolErrorKind.VALIDATION]
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
                    )
                )
                continue

            pre_validated.append((i, tool_obj, validated_args))

        # -- Phase 2: execute only the validated tool calls --------------
        for i, tool_obj, validated_args in pre_validated:
            tc_req = parsed_calls[i]
            tool_name = tc_req.name
            tool_tags = self._get_tool_tags(tool_name)

            # -- Approval check --
            approval_result = await self._check_approval(tool_obj, validated_args)
            if approval_result == PermissionLevel.DENY:
                hint = _ERROR_KIND_HINTS[ToolErrorKind.PERMISSION]
                deny_msg = f"Error: permission denied for tool '{tool_name}'\n\n{hint}"
                actions_taken.append(f"Denied: {tool_name}")
                tool_call_records.append(
                    StoredToolInteraction(
                        tool_call_id=tc_req.id,
                        name=tool_name,
                        args=validated_args,
                        result=deny_msg,
                        is_error=True,
                        tags=set(tool_tags),
                    )
                )
                tool_results.append(
                    ToolResultMessage(
                        tool_call_id=tc_req.id,
                        content=deny_msg,
                    )
                )
                continue

            await self._emit(ToolExecutionStartEvent(tool_name=tool_name, arguments=validated_args))
            tool_start = time.monotonic()
            result_str = ""
            is_error = False
            try:
                result = await tool_obj.function(**validated_args)
                result_str = result.content
                is_error = result.is_error
                if is_error:
                    hint = build_error_hint(result)
                    result_str += "\n\n" + hint
                if is_error:
                    actions_taken.append(f"Failed: {tool_name}")
                else:
                    actions_taken.append(f"Called {tool_name}")
                tool_call_records.append(
                    StoredToolInteraction(
                        tool_call_id=tc_req.id,
                        name=tool_name,
                        args=validated_args,
                        result=result_str,
                        is_error=is_error,
                        tags=set(tool_tags),
                    )
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
            tool_results.append(
                ToolResultMessage(
                    tool_call_id=tc_req.id,
                    content=result_str,
                )
            )

        return tool_results

    async def process_message(
        self,
        message_context: str,
        conversation_history: list[AgentMessage] | None = None,
        system_prompt_override: str | None = None,
        temperature: float | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop."""
        agent_start_time = time.monotonic()
        logger.debug(
            "Agent starting for user %d, message length=%d, history=%d messages",
            self.user.id,
            len(message_context),
            len(conversation_history) if conversation_history else 0,
        )
        system_prompt = system_prompt_override or await self._build_system_prompt(message_context)
        await self._emit(
            AgentStartEvent(
                user_id=self.user.id,
                message_context=message_context,
            )
        )

        messages: list[AgentMessage] = [SystemMessage(content=system_prompt)]

        if conversation_history:
            messages.extend(conversation_history)

        messages.append(UserMessage(content=message_context))

        # Trim oldest conversation history if content exceeds the limit.
        # Uses the block-based trimmer which preserves tool-call/result pairing
        # and injects a summary of dropped messages.
        original_count = len(messages)
        messages = trim_messages(
            messages,
            target_tokens=MAX_INPUT_TOKENS,
            input_tokens=self._last_input_tokens or None,
        )
        trimmed_count = original_count - len(messages)
        if trimmed_count > 0:
            logger.warning(
                "Trimmed %d message(s) from conversation history (limit %d tokens)",
                trimmed_count,
                MAX_INPUT_TOKENS,
            )

        llm_kwargs: dict[str, Any] = {}
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[StoredToolInteraction] = []
        reply_text = ""

        for _round in range(MAX_TOOL_ROUNDS):
            logger.debug(
                "Round %d/%d starting, %d messages in context",
                _round,
                MAX_TOOL_ROUNDS,
                len(messages),
            )
            # Rebuild tool schemas each round so dynamically activated
            # specialist tools are visible to the LLM.
            tool_schemas = [tool_to_function_schema(t) for t in self.tools] if self.tools else None
            await self._emit(TurnStartEvent(round_number=_round, message_count=len(messages)))
            response = await self._call_llm_with_retry(messages, tool_schemas, llm_kwargs)
            purpose = "agent_main" if _round == 0 else "agent_followup"
            log_llm_usage(self.user.id, settings.llm_model, response, purpose)
            if response.usage and response.usage.input_tokens:
                self._last_input_tokens = response.usage.input_tokens
                logger.debug(
                    "LLM usage: input_tokens=%d output_tokens=%d",
                    response.usage.input_tokens,
                    response.usage.output_tokens or 0,
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
                )

            # Parse tool calls via shared parser
            parsed_raw = parse_tool_calls(response)
            if not parsed_raw:
                reply_text = get_response_text(response)
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

            # Execute the tool round (validate, approve, run)
            tool_results = await self._execute_tool_round(
                parsed_calls,
                parsed_raw,
                actions_taken,
                memories_saved,
                tool_call_records,
            )

            # Activate any specialist factories requested via list_capabilities.
            # New tool schemas will be picked up at the top of the next round.
            self._check_specialist_activations(parsed_calls)

            messages.extend(tool_results)
            await self._emit(TurnEndEvent(round_number=_round, has_more_tool_calls=True))
        else:
            # Max rounds reached -- use last response content
            reply_text = get_response_text(response)
            logger.debug("Max tool rounds (%d) reached, using last response", MAX_TOOL_ROUNDS)

        total_duration = (time.monotonic() - agent_start_time) * 1000
        logger.debug(
            "Agent finished for user %d in %.1fms, actions=%s, reply_length=%d",
            self.user.id,
            total_duration,
            actions_taken or "(none)",
            len(reply_text),
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
        )

    def _find_tool(self, name: str) -> Callable[..., Any] | None:
        """Find a registered tool by name."""
        tool = self._tools_by_name.get(name)
        return tool.function if tool else None
