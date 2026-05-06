"""LLM usage tracking helper.

Extracts token counts from amessages responses and persists them to the
``llm_usage_logs`` table for cost monitoring. Cost itself is computed
in ``LLMUsageStore.log`` via ``services.llm_pricing``.
"""

from __future__ import annotations

import logging

from any_llm.types.messages import MessageResponse

from backend.app.agent.stores import LLMUsageStore

logger = logging.getLogger(__name__)


async def log_llm_usage(
    user_id: str,
    model: str,
    response: MessageResponse,
    purpose: str,
    provider: str = "",
) -> None:
    """Extract token usage from an LLM response and save to the usage log.

    *provider* is the any-llm provider id (``"anthropic"``, ``"openai"``,
    ``"google"``, etc.) under which *model* was invoked. We thread it
    through rather than guessing from the model name so cost lookup,
    persistence, and downstream analytics all see the same authoritative
    string. Empty string is allowed for legacy callers that haven't been
    updated yet; cost lookup will fall through to autodetect in that case.
    """
    prompt_tokens = response.usage.input_tokens
    completion_tokens = response.usage.output_tokens
    total_tokens = prompt_tokens + completion_tokens

    cache_creation_input_tokens = response.usage.cache_creation_input_tokens
    cache_read_input_tokens = response.usage.cache_read_input_tokens

    try:
        store = LLMUsageStore(user_id)
        await store.log_async(
            model,
            prompt_tokens,
            completion_tokens,
            purpose,
            provider=provider,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
    except Exception:
        logger.exception("Failed to log LLM usage for user %s", user_id)
        return

    logger.info(
        "LLM usage logged: user=%s provider=%s model=%s purpose=%s "
        "tokens=%d cache_create=%s cache_read=%s",
        user_id,
        provider or "?",
        model,
        purpose,
        total_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    )
