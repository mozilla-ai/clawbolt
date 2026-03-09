"""LLM usage tracking helper.

Extracts token counts from amessages responses and persists them to
the per-user ``llm_usage.jsonl`` file for cost monitoring.
"""

from __future__ import annotations

import logging

from any_llm.types.messages import MessageResponse

from backend.app.agent.file_store import LLMUsageStore

logger = logging.getLogger(__name__)


def log_llm_usage(
    user_id: int,
    model: str,
    response: MessageResponse,
    purpose: str,
) -> None:
    """Extract token usage from an LLM response and save to the usage log.

    Appends to the user's ``llm_usage.jsonl`` file.
    """
    prompt_tokens = response.usage.input_tokens
    completion_tokens = response.usage.output_tokens
    total_tokens = prompt_tokens + completion_tokens

    try:
        store = LLMUsageStore(user_id)
        store.log(model, prompt_tokens, completion_tokens, purpose)
    except Exception:
        logger.exception("Failed to log LLM usage for user %d", user_id)
        return

    logger.info(
        "LLM usage logged: user=%d model=%s purpose=%s tokens=%d",
        user_id,
        model,
        purpose,
        total_tokens,
    )
