import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import json_repair
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

from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt, get_missing_optional_fields
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
CONTEXT_QUERY_MAX_LENGTH = 100
RATE_LIMIT_RETRY_DELAY = 2.0
# Keep the most recent N messages (plus system prompt) when trimming for context length
CONTEXT_TRIM_KEEP_RECENT = 4

# Conservative default; most models support 128K+ but we leave room for output
MAX_INPUT_TOKENS = 120_000


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return sum(len(str(m.get("content", ""))) // 4 for m in messages)


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


SYSTEM_PROMPT_TEMPLATE = """You are Backshop, an AI assistant for solo contractors.

## About {contractor_name}
{soul_prompt}

## Your Memory
{memory_context}

## Instructions
- Be concise and practical. Contractors are busy.
- When you learn new information (rates, clients, preferences), save it using the save_fact tool.
- When asked for an estimate, gather the details, generate the PDF, and send it back using send_media_reply.
- You can ONLY communicate via this chat. You cannot send emails, make phone calls, or contact clients directly.
- Always be helpful, friendly, and professional.
- Keep replies concise. Contractors are on the job site.

## Proactive Messaging
You will proactively reach out during business hours when something needs attention:
- A draft estimate has been sitting unsent for over 24 hours
- A scheduled checklist item is due
- A follow-up reminder or deadline is approaching
- You haven't heard from the contractor in a few days

## Recall Behavior
When the contractor asks a question about their business, clients, or past work:
1. Use recall_facts to search your memory for relevant information.
2. If you find relevant facts, use them to answer clearly and concisely.
3. If you don't find anything, say so honestly -- don't make things up.
4. If the question is about general knowledge (not their specific business), answer from your training.
5. For "what do you know about me?" questions, summarize key facts by category.
"""


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

    def register_tools(self, tools: list[Tool]) -> None:
        """Register available tools for this agent session."""
        self.tools = tools
        self._tools_by_name = {}
        for tool in tools:
            if tool.name in self._tools_by_name:
                logger.warning("Duplicate tool name registered: %s", tool.name)
            self._tools_by_name[tool.name] = tool

    async def _build_system_prompt(self, message_context: str) -> str:
        """Build the full system prompt with soul + memory."""
        soul_prompt = build_soul_prompt(self.contractor)
        memory_context = await build_memory_context(
            self.db,
            self.contractor.id,
            query=message_context[:CONTEXT_QUERY_MAX_LENGTH] if message_context else None,
        )
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            contractor_name=self.contractor.name or "Contractor",
            soul_prompt=soul_prompt,
            memory_context=memory_context or "(No memories saved yet)",
        )

        missing = get_missing_optional_fields(self.contractor)
        if missing:
            missing_str = " and ".join(missing)
            prompt += (
                f"\nNote: You haven't learned this contractor's {missing_str} yet. "
                "If the opportunity comes up naturally in conversation, "
                "try to learn and save these details.\n"
            )

        return prompt

    async def _call_llm_with_retry(
        self,
        messages: list[Any],
        tool_schemas: list[Any] | None,
        llm_kwargs: dict[str, Any],
    ) -> ChatCompletion:
        """Call acompletion with typed exception handling and retry logic.

        Handles RateLimitError (retry once after delay) and
        ContextLengthExceededError (trim history and retry once).
        ContentFilterError and AuthenticationError are re-raised with
        appropriate logging so the caller can produce a user-facing message.
        """
        try:
            return cast(
                ChatCompletion,
                await acompletion(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    messages=messages,
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
                    messages=messages,
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
            return cast(
                ChatCompletion,
                await acompletion(
                    model=settings.llm_model,
                    provider=settings.llm_provider,
                    api_base=settings.llm_api_base,
                    messages=trimmed,
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
        messages: list[Any],
    ) -> list[Any]:
        """Trim conversation messages to fit within context limits.

        Keeps the system prompt (first message) and the most recent messages.
        """
        if len(messages) <= CONTEXT_TRIM_KEEP_RECENT + 1:
            return messages
        # system prompt + last N messages
        return [messages[0], *messages[-(CONTEXT_TRIM_KEEP_RECENT):]]

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
        conversation_history: list[dict[str, str]] | None = None,
        system_prompt_override: str | None = None,
        temperature: float | None = None,
    ) -> AgentResponse:
        """Process a message through the agent loop."""
        system_prompt = system_prompt_override or await self._build_system_prompt(message_context)

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": message_context})

        # Trim oldest conversation history if estimated tokens exceed the limit
        original_count = len(messages)
        estimated = _estimate_tokens(messages)
        while estimated > MAX_INPUT_TOKENS and len(messages) > 2:
            # Remove the oldest conversation history message
            # (keep system prompt at [0] and latest user message at [-1])
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

        llm_kwargs: dict[str, Any] = {"user": str(self.contractor.id)}
        if temperature is not None:
            llm_kwargs["temperature"] = temperature

        actions_taken: list[str] = []
        memories_saved: list[dict[str, str]] = []
        tool_call_records: list[dict[str, Any]] = []
        reply_text = ""

        for _round in range(MAX_TOOL_ROUNDS):
            response = await self._call_llm_with_retry(messages, tool_schemas, llm_kwargs)

            choice = response.choices[0]

            tool_calls = getattr(choice.message, "tool_calls", None)
            if not tool_calls:
                reply_text = choice.message.content or ""
                break

            # Append the assistant message (with tool_calls) to conversation
            messages.append(choice.message.model_dump())

            tool_results: list[dict[str, str]] = []
            for tool_call in tool_calls:
                func = getattr(tool_call, "function", None)
                if func is None:
                    continue
                tool_name = func.name
                try:
                    tool_args = json_repair.loads(func.arguments)
                    if not isinstance(tool_args, dict):
                        raise ValueError(f"Expected dict, got {type(tool_args).__name__}")
                except (ValueError, TypeError):
                    logger.warning(
                        "Malformed tool arguments for %s: %s",
                        tool_name,
                        func.arguments[:200],
                    )
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Error: malformed arguments for {tool_name}",
                        }
                    )
                    actions_taken.append(f"Failed: {tool_name} (bad args)")
                    continue

                tool_obj = self._tools_by_name.get(tool_name)
                tool_func = tool_obj.function if tool_obj else None
                tool_tags = self._get_tool_tags(tool_name)
                result_str = ""
                is_error = False
                if tool_func and tool_obj:
                    # Validate arguments against Pydantic model if present
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
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result_str,
                            }
                        )
                        continue

                    try:
                        result = await tool_func(**validated_args)
                        if isinstance(result, ToolResult):
                            result_str = result.content
                            is_error = result.is_error
                            if is_error:
                                hint = _build_error_hint(result)
                                result_str += "\n\n" + hint
                        else:
                            result_str = str(result)
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
                        actions_taken.append(f"Failed: {tool_name}")
                else:
                    available = ", ".join(sorted(self._tools_by_name.keys()))
                    result_str = (
                        f'Error: unknown tool "{tool_name}".'
                        f" Available tools: {available}"
                        f"\n\n{_DEFAULT_ERROR_HINT}"
                    )

                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                    }
                )

            messages.extend(tool_results)
        else:
            # Max rounds reached -- use last response content
            reply_text = choice.message.content or ""

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
