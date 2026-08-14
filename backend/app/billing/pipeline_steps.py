"""Quota check pipeline steps for multi-user deployments.

Implemented as pipeline steps (not ASGI middleware) because the agent
runs in a BackgroundTask that ASGI middleware cannot intercept.

Runs on the async DB stack so quota enforcement does not block the
event loop on every authenticated request.
"""

import logging

from backend.app.agent.core import AgentResponse
from backend.app.agent.router import PipelineContext, PipelineStep
from backend.app.billing.quota import (
    check_free_tier_daily_cap,
    check_message_quota,
    check_token_quota,
    increment_message_count,
    increment_token_count,
)
from backend.app.database import db_session_async

logger = logging.getLogger(__name__)

QUOTA_EXCEEDED_MESSAGE = (
    "You've reached your monthly message limit. Email support@clawbolt.ai for help."
)


async def check_quota_step(ctx: PipelineContext) -> PipelineContext:
    """Check message quota before processing.

    If quota is exceeded, sends a notification and sets an error response.
    The guarded_run_agent_step will skip the LLM call when ctx.response
    is already set.
    """
    cid = ctx.user.id
    async with db_session_async() as db:
        if (
            not await check_message_quota(db, cid)
            or not await check_token_quota(db, cid)
            or not await check_free_tier_daily_cap(db, cid)
        ):
            ctx.response = AgentResponse(reply_text=QUOTA_EXCEEDED_MESSAGE, is_error_fallback=True)
    return ctx


async def guarded_run_agent_step(ctx: PipelineContext) -> PipelineContext:
    """Run the agent only if no response has been set (e.g. by quota check).

    The default run_agent_step unconditionally overwrites ctx.response, so
    it is guarded here to preserve quota-exceeded responses.
    """
    if ctx.response is not None:
        return ctx

    from backend.app.agent.router import run_agent_step

    return await run_agent_step(ctx)


async def track_usage_step(ctx: PipelineContext) -> PipelineContext:
    """Increment message and token counts after successful processing."""
    cid = ctx.user.id
    if ctx.response and not ctx.response.is_error_fallback:
        async with db_session_async() as db:
            await increment_message_count(db, cid)

            # Track token usage against the token quota
            total_tokens = ctx.response.total_input_tokens + ctx.response.total_output_tokens
            if total_tokens > 0:
                await increment_token_count(db, cid, total_tokens)

            await db.commit()
    return ctx


# ---------------------------------------------------------------------------
# Multi-user pipeline
# ---------------------------------------------------------------------------

_MULTI_USER_PIPELINE: list[PipelineStep] | None = None


def get_multi_user_pipeline() -> list[PipelineStep]:
    """Build the agent pipeline with quota checks injected.

    Derived from ``DEFAULT_PIPELINE`` via ``build_pipeline()`` so future
    changes to the default pipeline are inherited automatically.

    Injects check_quota_step before the agent and track_usage_step after
    persist. Uses guarded_run_agent_step instead of run_agent_step so the
    quota check's early-exit response survives.
    """
    global _MULTI_USER_PIPELINE
    if _MULTI_USER_PIPELINE is not None:
        return _MULTI_USER_PIPELINE

    from backend.app.agent.router import (
        build_pipeline,
        persist_outbound_step,
        run_agent_step,
    )

    _MULTI_USER_PIPELINE = build_pipeline(
        insert_before={run_agent_step: [check_quota_step]},
        replace={run_agent_step: guarded_run_agent_step},
        insert_after={persist_outbound_step: [track_usage_step]},
    )
    return _MULTI_USER_PIPELINE
