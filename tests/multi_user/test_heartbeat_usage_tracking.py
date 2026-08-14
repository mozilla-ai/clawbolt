"""Regression: heartbeat LLM spend is tracked against UsageQuota (#305).

Heartbeat Phase 1 and Phase 2 run outside the ingestion pipeline, so the
pipeline-based quota tracker in ``billing/pipeline_steps.py`` never sees
those calls. Premium registers a post-run hook with OSS that increments
``UsageQuota.messages_used`` (when a reply was sent) and
``UsageQuota.tokens_used`` (always, when tokens were spent). These tests
pin that behavior so the silent-spend-vector from #305 cannot regress.
"""

import datetime
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.core import AgentResponse
from backend.app.agent.heartbeat import (
    HeartbeatDecision,
    _heartbeat_usage_hooks,
    run_heartbeat_for_user,
)
from backend.app.models import UsageQuota, User
from backend.app.services.heartbeat_usage import (
    install_heartbeat_usage_hook,
    track_heartbeat_usage,
)


@pytest.fixture(autouse=True)
def _clean_hooks() -> Generator[None]:
    """Snapshot and restore the OSS usage-hook registry around each test."""
    snapshot = list(_heartbeat_usage_hooks)
    _heartbeat_usage_hooks.clear()
    yield
    _heartbeat_usage_hooks.clear()
    _heartbeat_usage_hooks.extend(snapshot)


@pytest_asyncio.fixture
async def async_test_quota(async_db: async_sessionmaker, async_test_user: User) -> UsageQuota:
    """Async peer of ``test_quota`` for tests routing writes through the async path."""
    now = datetime.datetime.now(datetime.UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    quota = UsageQuota(
        user_id=async_test_user.id,
        period_start=period_start,
        messages_used=0,
        messages_limit=1000,
        tokens_used=0,
        tokens_limit=1_000_000,
    )
    async with async_db() as db:
        db.add(quota)
        await db.commit()
        await db.refresh(quota)
        db.expunge(quota)
    return quota


class TestTrackHeartbeatUsage:
    """Unit tests for the tracker that wraps UsageQuota increments."""

    async def test_sent_reply_increments_message_and_tokens(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
        async_test_quota: UsageQuota,
    ) -> None:
        """A delivered heartbeat counts as a message and its tokens are summed."""
        await track_heartbeat_usage(
            async_test_user.id, input_tokens=800, output_tokens=200, sent_reply=True
        )

        async with async_db() as db:
            quota = (
                await db.execute(select(UsageQuota).where(UsageQuota.user_id == async_test_user.id))
            ).scalar_one()
        assert quota.messages_used == 1
        assert quota.tokens_used == 1000

    async def test_skip_only_tracks_tokens(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
        async_test_quota: UsageQuota,
    ) -> None:
        """Phase 1 skip paths still spend tokens but do not count as a message."""
        await track_heartbeat_usage(
            async_test_user.id, input_tokens=80, output_tokens=20, sent_reply=False
        )

        async with async_db() as db:
            quota = (
                await db.execute(select(UsageQuota).where(UsageQuota.user_id == async_test_user.id))
            ).scalar_one()
        assert quota.messages_used == 0
        assert quota.tokens_used == 100

    async def test_creates_quota_row_on_demand(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        """Users without an existing UsageQuota get one created before tracking."""
        await track_heartbeat_usage(
            async_test_user.id, input_tokens=50, output_tokens=50, sent_reply=True
        )

        async with async_db() as db:
            quota = (
                await db.execute(select(UsageQuota).where(UsageQuota.user_id == async_test_user.id))
            ).scalar_one_or_none()
        assert quota is not None
        assert quota.messages_used == 1
        assert quota.tokens_used == 100


class TestHookRegistration:
    """Tests that ``install_heartbeat_usage_hook`` wires the tracker into OSS."""

    def test_install_registers_tracker(self) -> None:
        install_heartbeat_usage_hook()
        assert track_heartbeat_usage in _heartbeat_usage_hooks


class TestHeartbeatEndToEnd:
    """End-to-end: OSS heartbeat run updates premium's UsageQuota via the hook."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.bus.OutboundMessage")
    @patch("backend.app.bus.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_heartbeat_run_updates_usage_quota(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        async_db: async_sessionmaker,
        async_test_user: User,
        async_test_quota: UsageQuota,
    ) -> None:
        """Full Phase 1 + Phase 2 run with the hook installed updates UsageQuota."""
        install_heartbeat_usage_hook()

        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run",
            tasks="Check QB",
            reasoning="due",
            input_tokens=150,
            output_tokens=50,
        )
        mock_execute.return_value = AgentResponse(
            reply_text="All good.",
            total_input_tokens=1800,
            total_output_tokens=200,
        )
        mock_bus.publish_outbound = AsyncMock()
        mock_get_conv.return_value = (MagicMock(), True)
        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store
        mock_hb_store = MagicMock()
        mock_hb_store.log_heartbeat = AsyncMock()
        # OSS gate (#1118) short-circuits when HEARTBEAT.md has no
        # actionable lines. Configure the mock with a real list-item line
        # so the run reaches the LLM call this test is exercising.
        mock_hb_store.read_heartbeat_md_async = AsyncMock(return_value="- check QB invoices")
        mock_heartbeat_store_cls.return_value = mock_hb_store

        await run_heartbeat_for_user(async_test_user, "telegram", "+15551234", 5)

        async with async_db() as db:
            quota = (
                await db.execute(select(UsageQuota).where(UsageQuota.user_id == async_test_user.id))
            ).scalar_one()
        assert quota.messages_used == 1
        assert quota.tokens_used == 2200  # 150 + 50 + 1800 + 200
