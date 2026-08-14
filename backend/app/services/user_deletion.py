"""Account deletion service: archive usage, release resources.

All database helpers here take an ``AsyncSession``.
"""

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.file_store import get_user_store
from backend.app.agent.memory_db import reset_memory_stores
from backend.app.agent.session_db import reset_session_stores
from backend.app.config import settings
from backend.app.models import (
    DeletedUserUsage,
    Subscription,
    UsageQuota,
    User,
    UserPermissionSet,
)

logger = logging.getLogger(__name__)


async def archive_usage(db: AsyncSession, user: User) -> None:
    """Archive aggregate usage before deletion to prevent quota-reset abuse."""
    quotas = (
        (await db.execute(select(UsageQuota).where(UsageQuota.user_id == user.id))).scalars().all()
    )
    total_messages = sum(q.messages_used for q in quotas)
    total_tokens = sum(q.tokens_used for q in quotas)

    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    plan = sub.plan if sub else "free"

    record = DeletedUserUsage(
        original_user_id=user.user_id,
        plan_at_deletion=plan,
        total_messages=total_messages,
        total_tokens=total_tokens,
    )
    db.add(record)


def cascade_delete_user_data(user_id: str) -> None:
    """Delete all user-generated data from the file store."""
    data_dir = Path(settings.data_dir) / str(user_id)
    if data_dir.exists():
        shutil.rmtree(data_dir)
        logger.info("Deleted file store data for user %s", user_id)


async def cascade_delete_multi_user_data(db: AsyncSession, user_id: str) -> None:
    """Delete the multi-user tables' rows for this user."""
    await db.execute(
        delete(UsageQuota)
        .where(UsageQuota.user_id == user_id)
        .execution_options(synchronize_session=False)
    )


async def delete_account(db: AsyncSession, user: User) -> None:
    """Full account deletion: archive, cascade delete, deactivate."""
    await archive_usage(db, user)
    cascade_delete_user_data(user.id)
    await cascade_delete_multi_user_data(db, user.id)

    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    if sub:
        sub.status = "canceled"

    # Soft-delete: clear PII and deactivate via user store.
    # user_text and heartbeat_text are cleared so that if the account is
    # ever reactivated (e.g. by an admin), the user starts fresh instead
    # of being detected as "already onboarded" by the heuristic in
    # is_onboarding_needed (which reads leftover user_text).
    #
    # OSS owns the canonical user-row update path. Keep this field list
    # aligned with the onboarding heuristics so a reactivated account
    # starts from a clean state.
    store = get_user_store()
    await store.update_async(
        user.id,
        phone="",
        soul_text="",
        user_text="",
        heartbeat_text="",
        onboarding_complete=False,
        heartbeat_opt_in=False,
        is_active=False,
    )

    await db.commit()
    logger.info("Account deleted for user %s", user.id)


async def purge_account(db: AsyncSession, user: User, admin_id: str | None = None) -> None:
    """Hard delete: physically remove the user row and every trace of the account.

    Unlike ``delete_account`` (which soft-deactivates and archives usage to
    prevent quota-reset abuse), purge removes the user entirely so they can
    re-onboard from scratch with the same identity. Intended for admin use
    against test accounts.
    """
    # Capture identifiers now: after db.commit() the User instance expires
    # and attribute access would trigger a refresh on a deleted row.
    user_pk = user.id
    user_external_id = user.user_id

    # Multi-user rows reference users.id without CASCADE, so they must be
    # deleted before the user row. OSS tables use ondelete="CASCADE", so
    # deleting the user row is sufficient for OSS-owned data.
    await db.execute(
        delete(UsageQuota)
        .where(UsageQuota.user_id == user_pk)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(Subscription)
        .where(Subscription.user_id == user_pk)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(DeletedUserUsage)
        .where(DeletedUserUsage.original_user_id == user_external_id)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(UserPermissionSet)
        .where(UserPermissionSet.user_id == user_pk)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(User).where(User.id == user_pk).execution_options(synchronize_session=False)
    )
    await db.commit()

    # External side effects run only after the DB commit succeeds.
    cascade_delete_user_data(user_pk)

    # Drop any cached per-user stores so a re-onboarded user with the same
    # identity doesn't pick up stale in-memory state.
    reset_session_stores()
    reset_memory_stores()

    logger.info(
        "Account purged: target=%s (%s) admin=%s",
        user_pk,
        user_external_id,
        admin_id or "unknown",
    )
