"""Tests for the $0-usage-row backfill script.

Exercises ``scripts.backfill_llm_costs.backfill_costs`` against the test
database: a priceable ``cost=0`` row gets repaired, an unpriceable one is
left alone, an already-costed row is never re-priced, and dry-run writes
nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.database import db_session_async
from backend.app.models import LLMUsageLog, User
from scripts.backfill_llm_costs import backfill_costs


async def _add_usage_row(
    user: User,
    *,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
) -> int:
    async with db_session_async() as db:
        row = LLMUsageLog(
            user_id=user.id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost=cost,
            purpose="agent_main",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _cost_of(row_id: int) -> Decimal:
    async with db_session_async() as db:
        return (
            await db.execute(select(LLMUsageLog.cost).where(LLMUsageLog.id == row_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_apply_reprices_zero_cost_row(test_user: User) -> None:
    """A $0 row for a now-priced model is updated in place."""
    row_id = await _add_usage_row(
        test_user,
        model="claude-opus-4-8",
        provider="anthropic",
        input_tokens=7100,
        output_tokens=530,
        cost=Decimal("0.000000"),
    )

    result = await backfill_costs(model="claude-opus-4-8", provider="anthropic", apply=True)

    assert result.updated == 1
    assert result.total_added > Decimal("0")
    assert await _cost_of(row_id) > Decimal("0")


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(test_user: User) -> None:
    """Dry-run reports the row but leaves the stored cost at $0."""
    row_id = await _add_usage_row(
        test_user,
        model="claude-opus-4-8",
        provider="anthropic",
        input_tokens=7100,
        output_tokens=530,
        cost=Decimal("0.000000"),
    )

    result = await backfill_costs(model="claude-opus-4-8", provider="anthropic", apply=False)

    assert result.updated == 1
    assert result.applied is False
    assert await _cost_of(row_id) == Decimal("0.000000")


@pytest.mark.asyncio
async def test_unpriceable_row_stays_zero(test_user: User) -> None:
    """A model genai-prices does not know is left at $0, not crashed on."""
    row_id = await _add_usage_row(
        test_user,
        model="not-a-real-model-99",
        provider="anthropic",
        input_tokens=1000,
        output_tokens=1000,
        cost=Decimal("0.000000"),
    )

    result = await backfill_costs(apply=True)

    assert result.updated == 0
    assert await _cost_of(row_id) == Decimal("0.000000")


@pytest.mark.asyncio
async def test_already_costed_row_is_untouched(test_user: User) -> None:
    """Rows that already carry a cost are never re-priced."""
    sentinel = Decimal("0.123456")
    row_id = await _add_usage_row(
        test_user,
        model="claude-opus-4-8",
        provider="anthropic",
        input_tokens=7100,
        output_tokens=530,
        cost=sentinel,
    )

    result = await backfill_costs(model="claude-opus-4-8", provider="anthropic", apply=True)

    assert result.updated == 0
    assert await _cost_of(row_id) == sentinel


@pytest.mark.asyncio
async def test_zero_token_row_is_skipped(test_user: User) -> None:
    """A $0 row with no tokens is not a mispricing; leave it alone."""
    row_id = await _add_usage_row(
        test_user,
        model="claude-opus-4-8",
        provider="anthropic",
        input_tokens=0,
        output_tokens=0,
        cost=Decimal("0.000000"),
    )

    result = await backfill_costs(model="claude-opus-4-8", provider="anthropic", apply=True)

    assert result.scanned == 0
    assert result.updated == 0
    assert await _cost_of(row_id) == Decimal("0.000000")
