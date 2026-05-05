"""Tests for the async API of ``LLMUsageStore`` (issue #1156).

Mirrors the sync ``LLMUsageStore.log`` for the ``log_async`` peer
added in the dual-API rollout. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Follows the IdempotencyStore pilot pattern from
PR #1199.

This store matters for the request hot path: premium reads from
``llm_usage_logs`` for quota enforcement, and the sync write was a
known event-loop blocking risk.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.stores import LLMUsageStore
from backend.app.models import LLMUsageLog, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker, user_id: str) -> int:
    """Return the number of LLMUsageLog rows for the given user."""
    async with factory() as db:
        return (
            await db.scalar(
                select(func.count(LLMUsageLog.id)).where(LLMUsageLog.user_id == user_id)
            )
        ) or 0


async def _fetch_one(factory: async_sessionmaker, user_id: str) -> LLMUsageLog:
    """Return the single LLMUsageLog row for the given user."""
    async with factory() as db:
        return (
            (await db.execute(select(LLMUsageLog).where(LLMUsageLog.user_id == user_id)))
            .scalars()
            .one()
        )


# ---------------------------------------------------------------------------
# log_async
# ---------------------------------------------------------------------------


async def test_async_log_inserts_row(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``log_async`` inserts a single row visible via an async read."""
    store = LLMUsageStore(async_test_user.id)
    before = await _row_count(async_db, async_test_user.id)

    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=100,
        completion_tokens=50,
        purpose="agent",
        provider="anthropic",
    )

    assert await _row_count(async_db, async_test_user.id) == before + 1


async def test_async_log_persists_token_and_cost_columns(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``log_async`` populates the same columns as the sync ``log``.

    Maps prompt_tokens -> input_tokens, completion_tokens -> output_tokens.
    """
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=100,
        completion_tokens=50,
        purpose="agent",
        provider="anthropic",
    )

    row = await _fetch_one(async_db, async_test_user.id)
    assert row.model == "claude-sonnet-4-5"
    assert row.provider == "anthropic"
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.total_tokens == 150
    assert row.purpose == "agent"
    # Cost is computed via genai-prices; just assert the column was populated
    # with a non-negative numeric value.
    assert row.cost is not None
    assert float(row.cost) >= 0


async def test_async_log_persists_cache_token_columns(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``log_async`` writes through optional cache token columns."""
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=10,
        completion_tokens=5,
        purpose="agent",
        provider="anthropic",
        cache_creation_input_tokens=42,
        cache_read_input_tokens=7,
    )

    row = await _fetch_one(async_db, async_test_user.id)
    assert row.cache_creation_input_tokens == 42
    assert row.cache_read_input_tokens == 7


async def test_async_log_unknown_model_does_not_raise(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """An unknown (provider, model) combo logs a once-per-process warning, no raise.

    Mirrors the sync method's contract: cost falls through as 0 and the
    insert still happens.
    """
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="totally-made-up-model-name",
        prompt_tokens=10,
        completion_tokens=5,
        purpose="agent",
        provider="totally-made-up-provider",
    )

    row = await _fetch_one(async_db, async_test_user.id)
    assert row.model == "totally-made-up-model-name"
    assert row.provider == "totally-made-up-provider"
    assert row.input_tokens == 10
    assert row.output_tokens == 5


async def test_async_log_zero_tokens_persisted(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Zero-token logs still insert a row (matches sync)."""
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=0,
        completion_tokens=0,
        purpose="ping",
        provider="anthropic",
    )

    row = await _fetch_one(async_db, async_test_user.id)
    assert row.input_tokens == 0
    assert row.output_tokens == 0
    assert row.total_tokens == 0


# ---------------------------------------------------------------------------
# Sync/async parity: an async log is queryable via the same SQL the
# sync code uses for quota checks.
# ---------------------------------------------------------------------------


async def test_async_log_visible_via_async_select(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """A simple aggregate over the async-written rows returns the expected sum."""
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=100,
        completion_tokens=200,
        purpose="agent",
        provider="anthropic",
    )
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=50,
        completion_tokens=10,
        purpose="agent",
        provider="anthropic",
    )

    async with async_db() as db:
        total = await db.scalar(
            select(func.sum(LLMUsageLog.total_tokens)).where(
                LLMUsageLog.user_id == async_test_user.id
            )
        )

    # 100 + 200 + 50 + 10 = 360
    assert total == 360


# ---------------------------------------------------------------------------
# Per-test isolation canary
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 1: insert a usage row; the paired test below must not see it."""
    store = LLMUsageStore(async_test_user.id)
    await store.log_async(
        model="claude-sonnet-4-5",
        prompt_tokens=1,
        completion_tokens=1,
        purpose="agent",
        provider="anthropic",
    )
    assert await _row_count(async_db, async_test_user.id) == 1


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    assert await _row_count(async_db, async_test_user.id) == 0
