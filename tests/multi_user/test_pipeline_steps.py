"""Tests for premium pipeline steps (quota check, guarded agent, usage tracking).

Pipeline steps run on the async DB stack (issue #397), so setup goes through
the ``async_db`` fixture's connection. Mixing sync ``test_quota`` /
``test_subscription`` fixtures with async-route reads is a trap: the two
per-test transactions live on independent connections under READ COMMITTED,
so a row committed via ``SessionLocal()`` is invisible to an async read in the
same test. See the design comment block in ``tests/conftest.py``.
"""

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.core import AgentResponse
from backend.app.billing.pipeline_steps import (
    QUOTA_EXCEEDED_MESSAGE,
    check_quota_step,
    guarded_run_agent_step,
    track_usage_step,
)
from backend.app.models import Subscription, UsageQuota, User


def _period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _seed_subscription_and_quota(
    async_db: async_sessionmaker,
    user_id: str,
    *,
    role: str = "user",
    plan: str = "free",
    messages_used: int = 0,
    messages_limit: int = 1000,
    tokens_used: int = 0,
    tokens_limit: int = 1_000_000,
) -> None:
    """Insert a Subscription + UsageQuota row through the async per-test transaction."""
    async with async_db() as db:
        db.add(Subscription(user_id=user_id, role=role, plan=plan, status="active"))
        db.add(
            UsageQuota(
                user_id=user_id,
                period_start=_period_start(),
                messages_used=messages_used,
                messages_limit=messages_limit,
                tokens_used=tokens_used,
                tokens_limit=tokens_limit,
            )
        )
        await db.commit()


def _make_ctx(
    user: User,
    response: AgentResponse | None = None,
) -> MagicMock:
    """Create a mock PipelineContext."""
    ctx = MagicMock()
    ctx.user = user
    ctx.response = response
    ctx.to_address = "12345"
    return ctx


class TestCheckQuotaStep:
    async def test_allows_when_under_quota(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Should pass through when messages are under limit."""
        await _seed_subscription_and_quota(async_db, async_test_user.id)

        ctx = _make_ctx(async_test_user)
        result = await check_quota_step(ctx)
        assert result.response is None

    async def test_blocks_when_quota_exceeded(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Should set error response with quota message when quota exceeded."""
        await _seed_subscription_and_quota(
            async_db,
            async_test_user.id,
            role="user",
            messages_used=1000,
            messages_limit=1000,
        )

        ctx = _make_ctx(async_test_user)
        result = await check_quota_step(ctx)
        assert result.response is not None
        assert result.response.is_error_fallback is True
        assert result.response.reply_text == QUOTA_EXCEEDED_MESSAGE

    async def test_admin_bypasses_quota(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Admin users should never be blocked by quota checks (#211)."""
        await _seed_subscription_and_quota(
            async_db,
            async_test_user.id,
            role="admin",
            messages_used=1000,
            messages_limit=1000,
            tokens_used=1_000_000,
            tokens_limit=1_000_000,
        )

        ctx = _make_ctx(async_test_user)
        result = await check_quota_step(ctx)
        assert result.response is None


class TestGuardedRunAgentStep:
    async def test_skips_when_response_already_set(
        self,
        async_test_user: User,
    ) -> None:
        """Should not call agent when response is already set (quota exceeded)."""
        existing_response = AgentResponse(reply_text="blocked", is_error_fallback=True)
        ctx = _make_ctx(async_test_user, response=existing_response)

        result = await guarded_run_agent_step(ctx)
        assert result.response is existing_response

    async def test_runs_agent_when_no_response(
        self,
        async_test_user: User,
    ) -> None:
        """Should call run_agent_step when no response is set."""
        ctx = _make_ctx(async_test_user)
        agent_response = AgentResponse(reply_text="hello")

        async def mock_agent(c: Any) -> Any:
            c.response = agent_response
            return c

        with patch(
            "backend.app.agent.router.run_agent_step",
            side_effect=mock_agent,
        ):
            result = await guarded_run_agent_step(ctx)
            assert result.response is agent_response


class TestTrackUsageStep:
    async def test_increments_on_success(
        self,
        async_test_user: User,
    ) -> None:
        """Should increment message count on successful response."""
        response = AgentResponse(reply_text="done", is_error_fallback=False)
        ctx = _make_ctx(async_test_user, response=response)

        with patch(
            "backend.app.billing.pipeline_steps.increment_message_count",
            new_callable=AsyncMock,
        ) as mock_inc:
            await track_usage_step(ctx)
            mock_inc.assert_called_once()

    async def test_skips_on_error_fallback(
        self,
        async_test_user: User,
    ) -> None:
        """Should not increment when response is error fallback."""
        response = AgentResponse(reply_text="", is_error_fallback=True)
        ctx = _make_ctx(async_test_user, response=response)

        with patch(
            "backend.app.billing.pipeline_steps.increment_message_count",
            new_callable=AsyncMock,
        ) as mock_inc:
            await track_usage_step(ctx)
            mock_inc.assert_not_called()

    async def test_skips_on_no_response(
        self,
        async_test_user: User,
    ) -> None:
        """Should not increment when no response is set."""
        ctx = _make_ctx(async_test_user)

        with patch(
            "backend.app.billing.pipeline_steps.increment_message_count",
            new_callable=AsyncMock,
        ) as mock_inc:
            await track_usage_step(ctx)
            mock_inc.assert_not_called()
