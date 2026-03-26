import asyncio
import logging
import random
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
    PlanStep,
    format_plan_message,
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
from backend.app.agent.system_prompt import build_agent_system_prompt, build_time_user_context
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
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext, ToolRegistry
from backend.app.agent.trimming import trim_messages
from backend.app.config import settings
from backend.app.models import User
from backend.app.services.llm_service import reasoning_effort_to_thinking
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


@dataclass
class AgentResponse:
    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    memories_saved: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[StoredToolInteraction] = field(default_factory=list)
    is_error_fallback: bool = False
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    system_prompt: str = ""


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
        self._excluded_tool_names = excluded_tool_names
        self._request_id = request_id
        self._reactive_trim_dropped: list[AgentMessage] = []

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

    async def _persist_approval_prompt(self, prompt: str) -> None:
        """Store the approval prompt as an outbound message in the session.

        This ensures the prompt is visible in the web UI when viewing
        session history, even if the conversation originated on a
        different channel (e.g. iMessage or Telegram).
        """
        try:
            from backend.app.agent.session_db import get_session_store
            from backend.app.enums import MessageDirection

            session_store = get_session_store(self.user.id)
            await session_store.add_message_by_session_id(
                session_id=self._session_id,
                direction=MessageDirection.OUTBOUND,
                body=prompt,
            )
        except Exception:
            logger.warning("Failed to persist approval prompt for user %s", self.user.id)

    def _get_tool_permission(
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
            return PermissionLevel.AUTO, None, tool_obj.name

        resource: str | None = None
        if policy.resource_extractor is not None:
            resource = policy.resource_extractor(validated_args)

        store = get_approval_store()
        level = store.check_permission(
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
            excluded_tool_names=self._excluded_tool_names,
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
        system, msg_dicts = messages_to_messages_api(messages)
        tool_count = len(tool_schemas) if tool_schemas else 0
        thinking = reasoning_effort_to_thinking(settings.reasoning_effort)
        logger.debug(
            "Calling LLM: model=%s provider=%s messages=%d tools=%d max_tokens=%d",
            settings.llm_model,
            settings.llm_provider,
            len(msg_dicts),
            tool_count,
            effective_max_tokens,
        )
        for attempt in range(LLM_MAX_RETRIES):
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
                        max_tokens=effective_max_tokens,
                        thinking=thinking,
                        **llm_kwargs,
                    ),
                )
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
                system, trimmed_dicts = messages_to_messages_api(trim_result.messages)
                return cast(
                    MessageResponse,
                    await amessages(
                        model=settings.llm_model,
                        provider=settings.llm_provider,
                        api_base=settings.llm_api_base,
                        system=system,
                        messages=trimmed_dicts,
                        tools=tool_schemas,
                        max_tokens=effective_max_tokens,
                        thinking=thinking,
                        **llm_kwargs,
                    ),
                )
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

            pre_validated.append((i, tool_obj, validated_args))

        # -- Phase 2: batch approval then execute --------------------------
        #
        # Partition validated tools by permission level. Tools that need
        # approval are batched into a single plan message so the user
        # responds once (not once per tool).

        _ToolEntry = tuple[int, Tool, dict[str, Any]]

        auto_entries: list[_ToolEntry] = []
        ask_entries: list[tuple[_ToolEntry, str | None, str]] = []
        deny_entries: list[_ToolEntry] = []

        for entry in pre_validated:
            _i, tool_obj, v_args = entry
            level, resource, description = self._get_tool_permission(tool_obj, v_args)
            if level == PermissionLevel.AUTO:
                auto_entries.append(entry)
            elif level == PermissionLevel.DENY:
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
            auto_steps = [
                PlanStep(
                    tool_name=t.name,
                    description=self._get_tool_permission(t, a)[2],
                    level=PermissionLevel.AUTO,
                )
                for _, t, a in auto_entries
            ]
            ask_steps = [
                PlanStep(tool_name=e[1].name, description=desc, level=PermissionLevel.ASK)
                for e, _res, desc in ask_entries
            ]

            plan_msg = format_plan_message("Here's what I need to do:", auto_steps, ask_steps)

            # Persist the approval prompt to session history so it is
            # visible in the web UI regardless of which channel
            # originated the conversation.
            if self._session_id:
                await self._persist_approval_prompt(plan_msg)

            if self._publish_outbound is not None and self._chat_id is not None:
                # Publish approval prompt as SSE event for webchat clients.
                # The publish_outbound path works for Telegram but is a no-op
                # for webchat (WebChatChannel.send_text returns "").
                if self._request_id:
                    from backend.app.bus import message_bus

                    await message_bus.publish_event(
                        self._request_id,
                        {"type": "approval_request", "content": plan_msg},
                    )

                gate = get_approval_gate()
                decision = await gate.request_approval(
                    user_id=self.user.id,
                    tool_name=ask_entries[0][0][1].name,
                    description=plan_msg,
                    publish_outbound=self._publish_outbound,
                    channel=self._channel,
                    chat_id=self._chat_id,
                    prompt=plan_msg,
                )
            else:
                decision = ApprovalDecision.DENIED

            store = get_approval_store()
            if decision in (ApprovalDecision.APPROVED, ApprovalDecision.ALWAYS_ALLOW):
                approved_entries.extend(e for e, _res, _desc in ask_entries)
                if decision == ApprovalDecision.ALWAYS_ALLOW:
                    for (_, tool_obj, _a), resource, _desc in ask_entries:
                        try:
                            store.set_permission(
                                self.user.id, tool_obj.name, PermissionLevel.AUTO, resource
                            )
                        except Exception:
                            logger.warning("Failed to persist AUTO for tool %s", tool_obj.name)
            else:
                if decision == ApprovalDecision.ALWAYS_DENY:
                    for (_, tool_obj, _a), resource, _desc in ask_entries:
                        try:
                            store.set_permission(
                                self.user.id, tool_obj.name, PermissionLevel.DENY, resource
                            )
                        except Exception:
                            logger.warning("Failed to persist DENY for tool %s", tool_obj.name)

                for (idx, _tool_obj, v_args), _resource, _desc in ask_entries:
                    tc_req = parsed_calls[idx]
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

        # Execute all approved tools
        for i, tool_obj, validated_args in approved_entries:
            tc_req = parsed_calls[i]
            tool_name = tc_req.name
            tool_tags = self._get_tool_tags(tool_name)

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
                    is_error=is_error,
                )
            )

        return tool_results

    async def process_message(
        self,
        message_context: str,
        conversation_history: list[AgentMessage] | None = None,
        system_prompt_override: str | None = None,
        temperature: float | None = None,
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

        time_context = build_time_user_context(self.user)
        messages.append(UserMessage(content=f"{time_context}\n\n{message_context}"))

        # Trim oldest conversation history if content exceeds the limit.
        # Uses the block-based trimmer which preserves tool-call/result pairing
        # and injects a summary of dropped messages.
        original_count = len(messages)
        trim_result = trim_messages(
            messages,
            target_tokens=settings.context_trim_target_tokens,
            input_tokens=self._last_input_tokens or None,
        )
        messages = trim_result.messages
        all_dropped = list(trim_result.dropped)
        trimmed_count = original_count - len(messages)
        if trimmed_count > 0:
            logger.warning(
                "Trimmed %d message(s) from conversation history (limit %d tokens)",
                trimmed_count,
                settings.context_trim_target_tokens,
            )

        llm_kwargs: dict[str, Any] = {}
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[StoredToolInteraction] = []
        reply_text = ""
        _empty_reply_retried = False
        _total_input_tokens = 0
        _total_output_tokens = 0

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
            response = await self._call_llm_with_retry(
                messages, tool_schemas, llm_kwargs, max_tokens=max_tokens
            )
            purpose = "agent_main" if _round == 0 else "agent_followup"
            log_llm_usage(self.user.id, settings.llm_model, response, purpose)
            if response.usage and response.usage.input_tokens:
                self._last_input_tokens = response.usage.input_tokens
                _total_input_tokens += response.usage.input_tokens
                _total_output_tokens += response.usage.output_tokens or 0
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
                # Compact any messages already dropped before early return
                if self._reactive_trim_dropped:
                    all_dropped.extend(self._reactive_trim_dropped)
                    self._reactive_trim_dropped = []
                if all_dropped:
                    from backend.app.agent.context import trigger_compaction_for_dropped

                    trigger_compaction_for_dropped(self.user.id, all_dropped)

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
                    system_prompt=system_prompt,
                )

            # Parse tool calls via shared parser
            parsed_raw = parse_tool_calls(response)
            if not parsed_raw:
                reply_text = get_response_text(response)

                # If the LLM returned empty text after executing tools, re-prompt
                # once. This handles the case where the model performed work
                # (e.g. read_file during onboarding) but did not produce a
                # user-facing reply.
                if not reply_text and actions_taken and not _empty_reply_retried:
                    _empty_reply_retried = True
                    logger.debug(
                        "Round %d: empty reply after tool execution, re-prompting",
                        _round,
                    )
                    messages.append(
                        UserMessage(
                            content=(
                                "[System: you called tools but did not send a text reply. "
                                "If you intentionally chose not to respond "
                                "(e.g. the user asked for silence), return empty text. "
                                "Otherwise, please reply to the user.]"
                            )
                        )
                    )
                    await self._emit(TurnEndEvent(round_number=_round, has_more_tool_calls=True))
                    continue

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
            # enough room to generate the full tool call payload.
            if response_truncated and any(r.is_error for r in tool_results):
                effective = max_tokens or settings.llm_max_tokens_agent
                max_tokens = min(effective * 2, 4096)
                logger.info(
                    "Response truncated with errors, increasing max_tokens to %d",
                    max_tokens,
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

        # Collect any messages dropped by reactive trimming (ContextLengthExceededError)
        if self._reactive_trim_dropped:
            all_dropped.extend(self._reactive_trim_dropped)
            self._reactive_trim_dropped = []

        # Trigger background compaction for all dropped messages
        if all_dropped:
            from backend.app.agent.context import trigger_compaction_for_dropped

            trigger_compaction_for_dropped(self.user.id, all_dropped)

        total_duration = (time.monotonic() - agent_start_time) * 1000
        logger.debug(
            "Agent finished for user %s in %.1fms, actions=%s, reply_length=%d",
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
            total_input_tokens=_total_input_tokens,
            total_output_tokens=_total_output_tokens,
            system_prompt=system_prompt,
        )

    def _find_tool(self, name: str) -> Callable[..., Any] | None:
        """Find a registered tool by name."""
        tool = self._tools_by_name.get(name)
        return tool.function if tool else None
