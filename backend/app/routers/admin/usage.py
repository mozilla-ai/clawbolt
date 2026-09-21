"""Admin console: per-user quota and spend readout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.billing.cost import get_user_cost_totals
from backend.app.billing.quota import (
    get_current_quota,
)
from backend.app.database import get_async_db
from backend.app.models import (
    User,
)
from backend.app.query_helpers import iso_or_none
from backend.app.schemas.auth import AdminUsageSummary, UsageBucket
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)

router = APIRouter()


@router.get("/usage/{user_id}", response_model=AdminUsageSummary)
async def get_user_usage(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USAGE)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUsageSummary:
    """Get quota usage and aggregate LLM spend for a specific user.

    Existence-checks the user up-front. ``get_usage_summary`` calls
    ``get_current_quota`` which inserts a ``UsageQuota`` row for the
    user; without the check, a bogus ``user_id`` reaches that insert
    and 500s on the FK to ``users.id``. 404 is the right answer for an
    admin endpoint queried with an unknown user.

    Cost totals are scoped to the same period as the quota counters
    (``period_cost_usd`` covers the current calendar month, matching
    ``messages.used`` / ``tokens.used``). ``lifetime_cost_usd`` lets
    the admin spot a user whose monthly spend is fine but whose
    all-time spend is an outlier.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id

    user_exists = (
        await db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none() is not None
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    quota = await get_current_quota(db, user_id)
    costs = await get_user_cost_totals(db, user_id, quota.period_start)
    return AdminUsageSummary(
        messages=UsageBucket(used=quota.messages_used, limit=quota.messages_limit),
        tokens=UsageBucket(used=quota.tokens_used, limit=quota.tokens_limit),
        period_start=iso_or_none(quota.period_start),
        **costs,
    )
