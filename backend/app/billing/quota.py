"""Async usage quota enforcement and monthly reset.

Quota access now runs on the async DB stack only. Public helpers expose a
single async API, and the shared ``_*`` query builders keep the SQL
construction separate from session usage.
"""

import datetime

from sqlalchemy import Select, Update, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.billing.plans import get_plan_limits
from backend.app.config import settings
from backend.app.models import Subscription, UsageQuota


def _current_period_start() -> datetime.datetime:
    """Return the start of the current month (UTC).

    Quota periods align to calendar months (1st of each month, 00:00 UTC).
    Every user resets on the same day, which keeps the cap model simple
    to reason about and debug.
    """
    now = datetime.datetime.now(datetime.UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------


def _quota_select(user_id: str, period_start: datetime.datetime) -> Select[tuple[UsageQuota]]:
    return select(UsageQuota).where(
        UsageQuota.user_id == user_id,
        UsageQuota.period_start == period_start,
    )


def _subscription_select(user_id: str) -> Select[tuple[Subscription]]:
    return select(Subscription).where(Subscription.user_id == user_id)


def _increment_messages_update(user_id: str, period_start: datetime.datetime) -> Update:
    return (
        update(UsageQuota)
        .where(
            UsageQuota.user_id == user_id,
            UsageQuota.period_start == period_start,
        )
        .values(messages_used=UsageQuota.messages_used + 1)
    )


def _increment_tokens_update(user_id: str, period_start: datetime.datetime, tokens: int) -> Update:
    return (
        update(UsageQuota)
        .where(
            UsageQuota.user_id == user_id,
            UsageQuota.period_start == period_start,
        )
        .values(tokens_used=UsageQuota.tokens_used + tokens)
    )


def _reset_quota_update(user_id: str, period_start: datetime.datetime) -> Update:
    return (
        update(UsageQuota)
        .where(
            UsageQuota.user_id == user_id,
            UsageQuota.period_start == period_start,
        )
        .values(messages_used=0, tokens_used=0)
    )


def _free_tier_daily_count_select(period_start: datetime.datetime) -> Select[tuple[int]]:
    return (
        select(func.coalesce(func.sum(UsageQuota.messages_used), 0))
        .join(Subscription, Subscription.user_id == UsageQuota.user_id)
        .where(
            Subscription.plan == "free",
            Subscription.status == "active",
            UsageQuota.period_start == period_start,
        )
    )


def _new_quota_for(user_id: str, period_start: datetime.datetime, plan_name: str) -> UsageQuota:
    limits = get_plan_limits(plan_name)
    return UsageQuota(
        user_id=user_id,
        period_start=period_start,
        messages_used=0,
        messages_limit=limits.messages_per_month,
        tokens_used=0,
        tokens_limit=limits.tokens_per_month,
    )


def _missing_after_integrity_error(user_id: str) -> RuntimeError:
    return RuntimeError(f"UsageQuota not found after IntegrityError for user {user_id}")


async def get_current_quota(db: AsyncSession, user_id: str) -> UsageQuota:
    """Return the current month's usage quota, creating one if needed.

    Uses a savepoint so a concurrent insert does not abort the caller's
    outer transaction.
    """
    period_start = _current_period_start()

    quota = (await db.execute(_quota_select(user_id, period_start))).scalar_one_or_none()
    if quota is not None:
        return quota

    sub = (await db.execute(_subscription_select(user_id))).scalar_one_or_none()
    plan_name = sub.plan if sub else "free"

    quota = _new_quota_for(user_id, period_start, plan_name)
    try:
        async with db.begin_nested():
            db.add(quota)
    except IntegrityError as exc:
        quota = (await db.execute(_quota_select(user_id, period_start))).scalar_one_or_none()
        if quota is None:
            raise _missing_after_integrity_error(user_id) from exc
    return quota


async def _is_admin(db: AsyncSession, user_id: str) -> bool:
    """Return True if the user has the admin role on their subscription."""
    sub = (await db.execute(_subscription_select(user_id))).scalar_one_or_none()
    return sub is not None and sub.role == "admin"


async def check_message_quota(db: AsyncSession, user_id: str) -> bool:
    """Return True if the user has message quota remaining."""
    if await _is_admin(db, user_id):
        return True
    quota = await get_current_quota(db, user_id)
    return quota.messages_used < quota.messages_limit


async def check_token_quota(db: AsyncSession, user_id: str) -> bool:
    """Return True if the user has token quota remaining."""
    if await _is_admin(db, user_id):
        return True
    quota = await get_current_quota(db, user_id)
    return quota.tokens_used < quota.tokens_limit


async def increment_message_count(db: AsyncSession, user_id: str) -> None:
    """Atomically increment message usage for the current period."""
    period_start = _current_period_start()
    await db.execute(_increment_messages_update(user_id, period_start))
    await db.flush()


async def increment_token_count(db: AsyncSession, user_id: str, tokens: int) -> None:
    """Atomically increment token usage for the current period."""
    period_start = _current_period_start()
    await db.execute(_increment_tokens_update(user_id, period_start, tokens))
    await db.flush()


async def reset_quota(db: AsyncSession, user_id: str) -> None:
    """Reset current month's usage counters to zero."""
    period_start = _current_period_start()
    await db.execute(_reset_quota_update(user_id, period_start))
    await db.flush()


async def apply_plan_limits_to_current_quota(
    db: AsyncSession, user_id: str, plan_name: str
) -> None:
    """Update the active month's quota row to reflect a new plan's caps.

    UsageQuota rows capture limits at creation time, so a plan change
    has no mid-month effect unless we rewrite the active row. Used by
    the admin plan-change endpoint to make a free->pro flip take effect
    immediately instead of waiting for the next monthly rollover. Does
    not touch ``messages_used`` / ``tokens_used``: counters carry over
    so a user partway through their old cap does not get a free reset.
    No-op if no row exists for the current period yet; the next call
    to ``get_current_quota`` will create one with the new caps.
    """
    period_start = _current_period_start()
    limits = get_plan_limits(plan_name)
    await db.execute(
        update(UsageQuota)
        .where(
            UsageQuota.user_id == user_id,
            UsageQuota.period_start == period_start,
        )
        .values(
            messages_limit=limits.messages_per_month,
            tokens_limit=limits.tokens_per_month,
        )
    )
    await db.flush()


async def get_usage_summary(db: AsyncSession, user_id: str) -> dict:
    """Return current usage vs limits for a user."""
    quota = await get_current_quota(db, user_id)
    return {
        "messages": {"used": quota.messages_used, "limit": quota.messages_limit},
        "tokens": {"used": quota.tokens_used, "limit": quota.tokens_limit},
        "period_start": quota.period_start.isoformat() if quota.period_start else None,
    }


async def _get_free_tier_daily_message_count(db: AsyncSession) -> int:
    """Count messages across free-tier users for the current month."""
    period_start = _current_period_start()
    total = (await db.execute(_free_tier_daily_count_select(period_start))).scalar_one()
    return int(total)


async def check_free_tier_daily_cap(db: AsyncSession, user_id: str) -> bool:
    """Return True if the free-tier daily global cap allows another message."""
    cap = settings.free_tier_daily_global_cap
    if cap <= 0:
        return True

    sub = (await db.execute(_subscription_select(user_id))).scalar_one_or_none()
    if sub and sub.role == "admin":
        return True
    if sub and sub.plan != "free":
        return True

    return await _get_free_tier_daily_message_count(db) < cap
