"""LLM usage tracking helper.

Extracts token counts from acompletion responses and persists them to the
``llm_usage_logs`` table for cost monitoring per contractor.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import LLMUsageLog

logger = logging.getLogger(__name__)


def log_llm_usage(
    db: Session,
    contractor_id: int,
    model: str,
    response: Any,
    purpose: str,
) -> LLMUsageLog | None:
    """Extract token usage from an LLM response and save to the database.

    Returns the created ``LLMUsageLog`` row, or ``None`` if the response
    did not contain usage information.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        logger.debug("No usage data in LLM response for purpose=%s", purpose)
        return None

    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)

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
