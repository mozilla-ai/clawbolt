"""Tests for quota enforcement."""

import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.billing.quota import (
    check_message_quota,
    check_token_quota,
    get_current_quota,
    get_usage_summary,
    increment_message_count,
    increment_token_count,
)
from backend.app.models import Subscription, UsageQuota, User


def _period_start() -> datetime.datetime:
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _seed(
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
    """Insert Subscription + UsageQuota through the async per-test transaction."""
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


class TestQuotaEnforcement:
    async def test_check_message_quota_under_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(async_db, async_test_user.id)
        async with async_db() as db:
            assert await check_message_quota(db, async_test_user.id) is True

    async def test_check_message_quota_at_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(async_db, async_test_user.id, messages_used=1000, messages_limit=1000)
        async with async_db() as db:
            assert await check_message_quota(db, async_test_user.id) is False

    async def test_increment_message_count(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(async_db, async_test_user.id, messages_used=3)
        async with async_db() as db:
            await increment_message_count(db, async_test_user.id)
            await db.commit()
            summary = await get_usage_summary(db, async_test_user.id)
        assert summary["messages"]["used"] == 4

    async def test_increment_token_count(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(async_db, async_test_user.id)
        async with async_db() as db:
            await increment_token_count(db, async_test_user.id, 500)
            await db.commit()
            summary = await get_usage_summary(db, async_test_user.id)
        assert summary["tokens"]["used"] == 500

    async def test_get_current_quota_creates_new_period(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        """When no quota exists for current month, one is created from plan limits."""
        async with async_db() as db:
            db.add(
                Subscription(user_id=async_test_user.id, role="user", plan="free", status="active")
            )
            quota = await get_current_quota(db, async_test_user.id)
            messages_used = quota.messages_used
            messages_limit = quota.messages_limit
            await db.commit()

        from backend.app.billing.plans import PLANS

        assert messages_used == 0
        assert messages_limit == PLANS["free"].messages_per_month

    async def test_check_token_quota_allows_under_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            tokens_used=500,
            tokens_limit=1000,
        )
        async with async_db() as db:
            assert await check_token_quota(db, async_test_user.id) is True

    async def test_check_token_quota_blocks_at_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            tokens_used=1000,
            tokens_limit=1000,
        )
        async with async_db() as db:
            assert await check_token_quota(db, async_test_user.id) is False

    async def test_get_usage_summary(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(async_db, async_test_user.id)
        async with async_db() as db:
            summary = await get_usage_summary(db, async_test_user.id)
        assert summary["messages"]["used"] == 0
        assert summary["messages"]["limit"] == 1000
        assert "period_start" in summary


class TestAdminQuotaBypass:
    """Admin users should never be blocked by quota limits (#211)."""

    async def test_admin_bypasses_message_quota(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            role="admin",
            messages_used=1000,
            messages_limit=1000,
        )
        async with async_db() as db:
            assert await check_message_quota(db, async_test_user.id) is True

    async def test_admin_bypasses_token_quota(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            role="admin",
            tokens_used=1_000_000,
            tokens_limit=1_000_000,
        )
        async with async_db() as db:
            assert await check_token_quota(db, async_test_user.id) is True

    async def test_non_admin_still_blocked_at_message_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            role="user",
            messages_used=1000,
            messages_limit=1000,
        )
        async with async_db() as db:
            assert await check_message_quota(db, async_test_user.id) is False

    async def test_non_admin_still_blocked_at_token_limit(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        await _seed(
            async_db,
            async_test_user.id,
            role="user",
            tokens_used=1_000_000,
            tokens_limit=1_000_000,
        )
        async with async_db() as db:
            assert await check_token_quota(db, async_test_user.id) is False


class TestQuotaCreation:
    """Repeated current-month quota reads should return the same row."""

    async def test_reuses_existing_current_month_quota(
        self, async_db: async_sessionmaker, async_test_user: User
    ) -> None:
        async with async_db() as db:
            db.add(
                Subscription(user_id=async_test_user.id, role="user", plan="free", status="active")
            )
            await db.commit()

        async with async_db() as db:
            first = await get_current_quota(db, async_test_user.id)
            first_user_id = first.user_id
            first_period_start = first.period_start
            await db.commit()
        async with async_db() as db:
            second = await get_current_quota(db, async_test_user.id)
            second_user_id = second.user_id
            second_period_start = second.period_start
            await db.commit()

        assert first_user_id == second_user_id
        assert first_period_start == second_period_start
