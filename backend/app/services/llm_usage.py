"""LLM usage tracking helper.

Extracts token counts from amessages responses and persists them to the
``llm_usage_logs`` table for cost monitoring per contractor.
"""

from __future__ import annotations

import logging

from any_llm.types.messages import MessageResponse
from sqlalchemy.orm import Session

from backend.app.models import LLMUsageLog

logger = logging.getLogger(__name__)


def log_llm_usage(
    db: Session,
    contractor_id: int,
    model: str,
    response: MessageResponse,
    purpose: str,
) -> LLMUsageLog | None:
    """Extract token usage from an LLM response and save to the database.

    Returns the created ``LLMUsageLog`` row, or ``None`` if the response
    did not contain usage information.
    """
    prompt_tokens = response.usage.input_tokens
    completion_tokens = response.usage.output_tokens
    total_tokens = prompt_tokens + completion_tokens

    log_entry = LLMUsageLog(
        contractor_id=contractor_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        purpose=purpose,
    )
    try:
        db.add(log_entry)
        db.flush()
    except Exception:
        logger.exception("Failed to log LLM usage for contractor %d", contractor_id)
        db.rollback()
        return None

    logger.info(
        "LLM usage logged: contractor=%d model=%s purpose=%s tokens=%d",
        contractor_id,
        model,
        purpose,
        total_tokens,
    )
    return log_entry
