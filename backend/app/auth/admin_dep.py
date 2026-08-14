"""Shared admin authentication dependency.

Lives outside the admin router so the audit-log service can import it
without creating an import cycle.
"""

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import get_current_user
from backend.app.database import get_async_db
from backend.app.models import Subscription, User


async def get_current_admin(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> User:
    """Require admin privileges.

    Admin is granted exclusively by ``Subscription.role == "admin"`` in
    the database. The legacy ``ADMIN_USER_IDS`` env-var fallback was
    retired so that admin grants live in one place (audit-able, revocable,
    and consistent with the admin UI's "promote/demote" actions).

    Operators migrating from the env-var workflow should run the one-shot
    ``python -m backend.app.cli promote-env-admins`` command before
    removing ``ADMIN_USER_IDS`` from their environment.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    if sub and sub.role == "admin":
        return user
    raise HTTPException(status_code=403, detail="Admin access required")
