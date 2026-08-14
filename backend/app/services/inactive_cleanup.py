"""Inactive account cleanup (free tier only).

Thresholds are configurable via INACTIVE_WARN_MONTHS and INACTIVE_DELETE_MONTHS
environment variables (defaults: 11 and 12 months respectively).

Uses the ``last_login_at`` and ``inactivity_warned_at`` columns on the User
table for efficient querying.  Falls back
to session timestamps for users whose ``last_login_at`` is NULL (e.g. users
created before the column was added).
"""

import datetime
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.file_store import get_session_store
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.models import Subscription, User
from backend.app.services.user_deletion import delete_account

logger = logging.getLogger(__name__)


def _ensure_aware(dt: datetime.datetime) -> datetime.datetime:
    """Ensure a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt


async def _get_last_login_at(oss_db: AsyncSession, user_id: str) -> datetime.datetime | None:
    """Read last_login_at via raw SQL (column added dynamically, not an ORM attribute)."""
    row = (
        await oss_db.execute(
            text("SELECT last_login_at FROM users WHERE id = :uid"),
            {"uid": user_id},
        )
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _ensure_aware(row[0])


async def _get_inactivity_warned_at(oss_db: AsyncSession, user_id: str) -> datetime.datetime | None:
    """Read inactivity_warned_at via raw SQL."""
    row = (
        await oss_db.execute(
            text("SELECT inactivity_warned_at FROM users WHERE id = :uid"),
            {"uid": user_id},
        )
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _ensure_aware(row[0])


async def _get_last_activity(user: User, oss_db: AsyncSession) -> datetime.datetime | None:
    """Return the most recent activity timestamp for a user.

    Prefers ``last_login_at`` (indexed, fast) but falls back to session
    message timestamps for users that predate the column.
    """
    last_login = await _get_last_login_at(oss_db, user.id)
    if last_login is not None:
        return last_login

    session_store = get_session_store(user.id)
    last_inbound = await session_store.get_last_inbound_timestamp_async()
    if last_inbound is not None:
        return _ensure_aware(last_inbound)
    last_outbound = await session_store.get_last_outbound_timestamp_async()
    if last_outbound is not None:
        return _ensure_aware(last_outbound)
    return _ensure_aware(user.created_at)


async def get_inactive_free_users(
    db: AsyncSession, inactive_since: datetime.datetime
) -> list[User]:
    """Return free-tier users with no activity since the given cutoff."""
    # Get free-tier user IDs
    free_subs = (
        await db.execute(
            select(Subscription.user_id).where(
                Subscription.plan == "free",
                Subscription.status == "active",
            )
        )
    ).all()
    free_ids = {uid for (uid,) in free_subs}

    async with db_session_async() as oss_db:
        all_users = (
            (await oss_db.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
        )

        result: list[User] = []
        for user in all_users:
            if user.id not in free_ids:
                continue
            last_active = await _get_last_activity(user, oss_db)
            if (last_active is not None and last_active < inactive_since) or (
                last_active is None and _ensure_aware(user.created_at) < inactive_since
            ):
                result.append(user)

    return result


async def warn_inactive_users(db: AsyncSession) -> int:
    """Send warning emails to free-tier users inactive for 11+ months.

    Sets ``inactivity_warned_at`` so the user is only warned once.
    Returns the number of users warned.
    """
    now = datetime.datetime.now(datetime.UTC)
    warn_cutoff = now - datetime.timedelta(days=settings.inactive_warn_months * 30)
    delete_cutoff = now - datetime.timedelta(days=settings.inactive_delete_months * 30)

    inactive = await get_inactive_free_users(db, warn_cutoff)
    warned = 0

    async with db_session_async() as oss_db:
        for user in inactive:
            last_active = await _get_last_activity(user, oss_db)
            # Skip users already past the delete threshold (they'll be deleted)
            if last_active is not None and last_active < delete_cutoff:
                continue

            # Skip users already warned
            warned_at = await _get_inactivity_warned_at(oss_db, user.id)
            if warned_at is not None:
                continue

            logger.info(
                "Inactive account warning for user %s (last active: %s)",
                user.id,
                last_active,
            )

            # Mark as warned. ``inactivity_warned_at`` is TIMESTAMP WITHOUT
            # TIME ZONE; asyncpg refuses tz-aware datetimes for that column,
            # so strip tzinfo before binding (the value is already UTC).
            await oss_db.execute(
                text("UPDATE users SET inactivity_warned_at = :now WHERE id = :uid"),
                {"now": now.replace(tzinfo=None), "uid": user.id},
            )
            warned += 1

        if warned:
            await oss_db.commit()
            logger.info("Warned %d inactive free-tier users", warned)

    return warned


async def cleanup_inactive_accounts(db: AsyncSession) -> int:
    """Delete free-tier accounts inactive for 12+ months.

    Only deletes accounts that were warned at least 30 days ago (via the
    ``inactivity_warned_at`` column), giving users time to log back in.
    Returns the number of accounts deleted.
    """
    now = datetime.datetime.now(datetime.UTC)
    delete_cutoff = now - datetime.timedelta(days=settings.inactive_delete_months * 30)
    warning_grace = datetime.timedelta(days=30)

    inactive = await get_inactive_free_users(db, delete_cutoff)
    deleted = 0

    async with db_session_async() as oss_db:
        for user in inactive:
            # Require that a warning was sent at least 30 days ago
            warned_at = await _get_inactivity_warned_at(oss_db, user.id)
            if warned_at is None:
                continue
            if now - warned_at < warning_grace:
                continue

            logger.info(
                "Deleting inactive account for user %s (user_id=%s)",
                user.id,
                user.user_id,
            )
            # ``delete_account`` is async-DB-only after #395; open a short
            # async session for the deletion so it doesn't share a
            # transaction with the iteration's ``oss_db`` (which is also
            # writing to ``users``).
            async with db_session_async() as adb:
                await delete_account(adb, user)
            deleted += 1

    if deleted:
        logger.info("Deleted %d inactive free-tier accounts", deleted)
    return deleted
