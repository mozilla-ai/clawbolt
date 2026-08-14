"""Account management endpoints: profile, usage, export, delete."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.dependencies import get_current_user
from backend.app.billing.quota import get_usage_summary
from backend.app.database import get_async_db
from backend.app.models import Subscription, User
from backend.app.schemas import ProfileResponse, StatusResponse, UsageSummary
from backend.app.services.data_export import export_user_data
from backend.app.services.user_deletion import delete_account as _delete_account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> ProfileResponse:
    """Return the authenticated user's profile."""
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    return ProfileResponse(
        id=user.id,
        plan=sub.plan if sub else "free",
        role=sub.role if sub else "user",
    )


@router.get("/usage", response_model=UsageSummary)
async def get_account_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> UsageSummary:
    """Return current usage for the authenticated user."""
    return UsageSummary(**await get_usage_summary(db, user.id))


@router.get("/export")
async def export_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Export all user data as JSON for GDPR compliance."""
    return await export_user_data(db, user)


@router.delete("/delete", response_model=StatusResponse)
async def delete_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> StatusResponse:
    """Delete account: archive usage, cascade delete data, deactivate.

    Archives usage totals to prevent quota-reset abuse, deletes all
    user-generated data, and deactivates the user record.
    """
    await _delete_account(db, user)
    return StatusResponse(status="deleted")
