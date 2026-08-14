"""Aggregate LLM call cost per user.

Wraps the OSS ``llm_usage_logs`` table so the admin UI can answer
"how much has this user cost us this month?" without iterating the
paginated per-call logs endpoint client-side. Returns formatted
decimal strings (matching the per-row ``cost_usd`` shape on
``LLMUsageLogItem``) so JSON serialization does not silently drop
precision the way ``float`` would.
"""

import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import LLMUsageLog

# Match per-row ``cost_usd`` formatting in ``LLMUsageLogItem`` so the
# admin UI can render period totals next to per-row costs without a
# rounding mismatch (genai-prices cost column is ``Numeric(12, 6)``).
_COST_DECIMALS = 6
_ZERO_COST = Decimal(0)


def _format_cost(value: Decimal | None) -> str:
    resolved = value if value is not None else _ZERO_COST
    return f"{resolved:.{_COST_DECIMALS}f}"


async def get_user_cost_totals(
    db: AsyncSession, user_id: str, period_start: datetime.datetime
) -> dict[str, str]:
    """Return ``{period_cost_usd, lifetime_cost_usd}`` for one user.

    Two SUM queries rather than one CASE-WHEN for simpler SQL and
    clearer test assertions; both run against the
    ``llm_usage_logs.user_id`` index so the absolute cost is small
    at admin-page scale and not worth the conditional-aggregate
    trade-off. Postgres returns ``NULL`` from ``SUM`` when no rows
    match; ``_format_cost`` collapses that to ``"0.000000"`` so the
    JSON shape is always a parsable decimal string.
    """
    period_cost = (
        await db.execute(
            select(func.sum(LLMUsageLog.cost)).where(
                LLMUsageLog.user_id == user_id,
                LLMUsageLog.created_at >= period_start,
            )
        )
    ).scalar_one_or_none()
    lifetime_cost = (
        await db.execute(select(func.sum(LLMUsageLog.cost)).where(LLMUsageLog.user_id == user_id))
    ).scalar_one_or_none()
    return {
        "period_cost_usd": _format_cost(period_cost),
        "lifetime_cost_usd": _format_cost(lifetime_cost),
    }
