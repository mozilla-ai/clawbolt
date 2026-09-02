"""Single-round model dispatch for the evaluator.

Mirrors ``ClawboltAgent._call_llm_with_retry`` in everything that shapes the
request (cache breakpoints, system-prompt caching, tool caching, thinking
config) and deliberately drops everything that shapes the *conversation*:
no observers fire, no typing indicator is sent, no context-overflow retry
re-trims the prompt, and no usage is logged against the user's quota. An
evaluation must not appear in the user's spend or in the operator's
telemetry as if it were real traffic.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any, cast

from any_llm import amessages
from any_llm.exceptions import AnyLLMError
from any_llm.types.messages import MessageResponse

from backend.app.agent.core import AssembledPrompt
from backend.app.agent.llm_parsing import get_response_text, parse_tool_calls
from backend.app.agent.messages import messages_to_messages_api
from backend.app.config import settings
from backend.app.services.llm_eval.types import ModelCallResult, ToolCall
from backend.app.services.llm_service import (
    apply_history_cache_breakpoint,
    apply_in_turn_cache_breakpoint,
    apply_tool_caching,
    prepare_system_with_caching,
    reasoning_effort_to_thinking,
)

logger = logging.getLogger(__name__)


def _serialize_blocks(response: MessageResponse) -> list[dict[str, Any]]:
    """Dump response content blocks to plain JSON-safe dicts."""
    blocks: list[dict[str, Any]] = []
    for block in response.content:
        try:
            blocks.append(block.model_dump(mode="json"))
        except Exception:
            logger.exception("Failed to serialize an eval response block; skipping")
    return blocks


async def call_model(
    assembled: AssembledPrompt,
    tool_schemas: list[dict[str, Any]] | None,
    *,
    provider: str,
    model: str,
    max_tokens: int | None = None,
) -> ModelCallResult:
    """Send one assembled prompt to one model and return its first decision.

    Provider errors are captured onto the result rather than raised: one
    model failing on one turn is a data point about that model, not a
    reason to abandon a run that may be 90 turns deep.
    """
    effective_max_tokens = max_tokens or settings.llm_max_tokens_agent
    system_str, msg_dicts = messages_to_messages_api(assembled.messages)

    # The cache helpers stamp ``cache_control`` markers onto the dicts they
    # are given. Both models in a comparison are handed the same assembled
    # prompt, and the two providers may not agree on whether markers belong,
    # so each call works on its own copy. Without this the second model
    # would inherit the first's markers.
    msg_dicts = copy.deepcopy(msg_dicts)
    schemas = copy.deepcopy(tool_schemas) if tool_schemas else None

    msg_dicts = apply_history_cache_breakpoint(msg_dicts, provider)
    msg_dicts = apply_in_turn_cache_breakpoint(msg_dicts, provider)
    system: str | list[dict[str, Any]] | None = system_str
    if system is not None:
        system = prepare_system_with_caching(system, provider)
    if schemas:
        schemas = apply_tool_caching(schemas, provider)
    thinking = reasoning_effort_to_thinking(settings.reasoning_effort)

    started = time.monotonic()
    try:
        response = cast(
            MessageResponse,
            await amessages(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                system=system,
                messages=msg_dicts,
                tools=schemas,
                max_tokens=effective_max_tokens,
                thinking=thinking,
            ),
        )
    except AnyLLMError as exc:
        logger.warning("Eval call failed for %s/%s: %s", provider, model, exc)
        return ModelCallResult(
            provider=provider,
            model=model,
            latency_ms=(time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        logger.exception("Unexpected eval call failure for %s/%s", provider, model)
        return ModelCallResult(
            provider=provider,
            model=model,
            latency_ms=(time.monotonic() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = (time.monotonic() - started) * 1000
    usage = response.usage
    # ``arguments`` is None when the provider returned a tool input that was
    # not a dict. Recorded as empty so the args validator sees it and reports
    # ``invalid_args`` rather than the call silently vanishing from the diff.
    tool_calls = [
        ToolCall(name=c.name, arguments=c.arguments or {}) for c in parse_tool_calls(response)
    ]
    return ModelCallResult(
        provider=provider,
        model=model,
        text=get_response_text(response),
        tool_calls=tool_calls,
        content_blocks=_serialize_blocks(response),
        stop_reason=response.stop_reason,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        cache_creation_input_tokens=(usage.cache_creation_input_tokens or 0) if usage else 0,
        cache_read_input_tokens=(usage.cache_read_input_tokens or 0) if usage else 0,
        latency_ms=latency_ms,
    )
