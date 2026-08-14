"""Tests for free-tier daily global cap (DB-backed)."""

import datetime
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.billing import quota as quota_module
from backend.app.billing.quota import (
    check_free_tier_daily_cap,
    increment_message_count,
)
from backend.app.models import Subscription, UsageQuota, User


def _period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _seed_free_user(
    async_db: async_sessionmaker,
    user_id: str,
    *,
    messages_used: int = 0,
    plan: str = "free",
    role: str = "user",
) -> None:
    """Insert a Subscription + UsageQuota for the free-tier cap tests."""
    async with async_db() as db:
        db.add(Subscription(user_id=user_id, role=role, plan=plan, status="active"))
        db.add(
            UsageQuota(
                user_id=user_id,
                period_start=_period_start(),
                messages_used=messages_used,
                messages_limit=1000,
                tokens_used=0,
                tokens_limit=1_000_000,
            )
        )
        await db.commit()


class TestFreeTierDailyCap:
    async def test_disabled_when_cap_is_zero(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 0):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, "dummy") is True

    async def test_paid_user_bypasses_cap(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed_free_user(async_db, async_test_user.id, plan="pro")
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 1):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, async_test_user.id) is True

    async def test_free_user_blocked_at_cap(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed_free_user(async_db, async_test_user.id, messages_used=5)
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 5):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, async_test_user.id) is False

    async def test_free_user_allowed_under_cap(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed_free_user(async_db, async_test_user.id)
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 10):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, async_test_user.id) is True

    async def test_cap_persists_across_function_calls(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        """Daily cap is DB-backed: count persists without in-memory state (#200)."""
        await _seed_free_user(async_db, async_test_user.id)
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 2):
            async with async_db() as db:
                await increment_message_count(db, async_test_user.id)
                await increment_message_count(db, async_test_user.id)
                await db.commit()
                assert await check_free_tier_daily_cap(db, async_test_user.id) is False

    async def test_admin_bypasses_daily_cap(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed_free_user(async_db, async_test_user.id, role="admin", messages_used=100)
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 1):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, async_test_user.id) is True

    async def test_no_subscription_treated_as_free(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        """User with no subscription record is treated as free tier."""
        with patch.object(quota_module.settings, "free_tier_daily_global_cap", 1):
            async with async_db() as db:
                assert await check_free_tier_daily_cap(db, async_test_user.id) is True
