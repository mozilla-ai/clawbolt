"""Reflect OSS heartbeat LLM spend into per-tenant UsageQuota counters.

Heartbeat Phase 1 and Phase 2 LLM calls bypass the ingestion pipeline, so
the pipeline-based quota tracking in ``billing/pipeline_steps.py`` does not
see them. This module registers a hook with OSS that fires after each
heartbeat run and increments the tenant's message and token counters so
admin dashboards and monthly caps reflect true LLM usage.

Tracking-only per issue #305: heartbeats are not gated on quota.
"""

import logging

from backend.app.agent.heartbeat import register_heartbeat_usage_hook
from backend.app.billing.quota import (
    get_current_quota,
    increment_message_count,
    increment_token_count,
)
from backend.app.database import db_session_async

logger = logging.getLogger(__name__)


async def track_heartbeat_usage(
    user_id: str,
    input_tokens: int,
    output_tokens: int,
    sent_reply: bool,
) -> None:
    """Increment UsageQuota counters for a completed heartbeat run.

    Always called with at least one non-zero token count (OSS suppresses
    the hook when no LLM call fired). Counts the heartbeat as a "message"
    only when a reply was actually delivered, matching how inbound
    conversation turns are counted.
    """
    total_tokens = input_tokens + output_tokens
    try:
        async with db_session_async() as db:
            # Ensure the current-month UsageQuota row exists so subsequent
            # UPDATE statements have something to increment. Users without
            # a subscription still receive heartbeats post-#304 and need a
            # quota row created on demand.
            await get_current_quota(db, user_id)
            if sent_reply:
                await increment_message_count(db, user_id)
            if total_tokens > 0:
                await increment_token_count(db, user_id, total_tokens)
            await db.commit()
    except Exception:
        logger.exception("Failed to track heartbeat usage for user %s", user_id)


def install_heartbeat_usage_hook() -> None:
    """Register the tracker with the OSS heartbeat engine."""
    register_heartbeat_usage_hook(track_heartbeat_usage)
