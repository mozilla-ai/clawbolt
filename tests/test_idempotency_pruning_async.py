"""Tests for the async API of ``IdempotencyStore`` (issue #1150).

Mirrors ``tests/test_idempotency_pruning.py`` for the ``*_async`` peers
added in the dual-API rollout pilot. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Future per-store conversions (#1151-#1157) should
mirror this file when validating their async peers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.stores import IdempotencyStore
from backend.app.models import IdempotencyKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker) -> int:
    """Return the total number of IdempotencyKey rows in the DB."""
    async with factory() as db:
        return (await db.scalar(select(func.count(IdempotencyKey.id)))) or 0


async def _surviving_ids(factory: async_sessionmaker) -> set[str]:
    """Return the set of external_id values still present in the table."""
    async with factory() as db:
        rows = (await db.execute(select(IdempotencyKey.external_id))).all()
        return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_async_prune_removes_oldest_keeps_newest(
    async_db: async_sessionmaker,
) -> None:
    """``_prune_async`` deletes the oldest rows and keeps the newest up to the cap."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 5):
            assert await store.try_mark_seen_async(f"ext-{i}") is True

    # Oldest 5 pruned, newest 10 survive.
    for i in range(5):
        assert not await store.has_seen_async(f"ext-{i}")
    for i in range(5, small_max + 5):
        assert await store.has_seen_async(f"ext-{i}")


async def test_async_prune_deterministic_on_every_insert(
    async_db: async_sessionmaker,
) -> None:
    """Pruning fires on every insert, enforcing a hard cap."""
    store = IdempotencyStore()
    small_max = 20

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 50):
            await store.try_mark_seen_async(f"det-{i}")

    assert await _row_count(async_db) == small_max

    surviving = await _surviving_ids(async_db)
    for i in range(small_max + 50 - small_max, small_max + 50):
        assert f"det-{i}" in surviving


async def test_async_prune_noop_at_exact_max(
    async_db: async_sessionmaker,
) -> None:
    """``_prune_async`` is a no-op when row count equals exactly ``_SEEN_MAX``."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max):
            await store.try_mark_seen_async(f"exact-{i}")
        await store._prune()

    assert await _row_count(async_db) == small_max
    for i in range(small_max):
        assert await store.has_seen_async(f"exact-{i}")


async def test_async_prune_noop_when_below_max(
    async_db: async_sessionmaker,
) -> None:
    """``_prune_async`` is a no-op when row count is below ``_SEEN_MAX``."""
    store = IdempotencyStore()
    for i in range(5):
        await store.try_mark_seen_async(f"below-{i}")

    await store._prune()

    assert await _row_count(async_db) == 5
    for i in range(5):
        assert await store.has_seen_async(f"below-{i}")


async def test_async_prune_on_empty_table(
    async_db: async_sessionmaker,
) -> None:
    """``_prune_async`` on an empty table does not raise."""
    store = IdempotencyStore()
    await store._prune()
    assert await _row_count(async_db) == 0


async def test_async_prune_with_seen_max_one(
    async_db: async_sessionmaker,
) -> None:
    """``_SEEN_MAX = 1`` keeps only the latest row."""
    store = IdempotencyStore()

    with patch("backend.app.agent.stores._SEEN_MAX", 1):
        await store.try_mark_seen_async("first")
        await store.try_mark_seen_async("second")
        await store.try_mark_seen_async("third")

    assert await _row_count(async_db) == 1
    assert await store.has_seen_async("third")
    assert not await store.has_seen_async("first")
    assert not await store.has_seen_async("second")


async def test_async_duplicate_returns_false(
    async_db: async_sessionmaker,
) -> None:
    """Duplicate ``external_id`` returns ``False``."""
    store = IdempotencyStore()
    assert await store.try_mark_seen_async("dup-1") is True
    assert await store.try_mark_seen_async("dup-1") is False


async def test_async_prune_exception_does_not_block_return(
    async_db: async_sessionmaker,
) -> None:
    """If ``_prune_async`` raises, ``try_mark_seen_async`` still returns True."""
    store = IdempotencyStore()

    with patch.object(
        store,
        "_prune",
        new=AsyncMock(side_effect=RuntimeError("db exploded")),
    ):
        result = await store.try_mark_seen_async("safe-1")

    assert result is True
    assert await store.has_seen_async("safe-1")


async def test_async_mark_seen_alias(
    async_db: async_sessionmaker,
) -> None:
    """``mark_seen_async`` is the fire-and-forget peer of ``try_mark_seen_async``."""
    store = IdempotencyStore()
    await store.mark_seen_async("alias-1")
    assert await store.has_seen_async("alias-1")
    # No-op on second call (no exception, even though duplicate).
    await store.mark_seen_async("alias-1")
    assert await store.has_seen_async("alias-1")


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
) -> None:
    """Half 1 of a paired check that the async fixture rolls back between tests.

    Writes a row; the paired test below must not see it.
    """
    store = IdempotencyStore()
    assert await store.try_mark_seen_async("iso-canary") is True
    assert await store.has_seen_async("iso-canary") is True


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = IdempotencyStore()
    assert await store.has_seen_async("iso-canary") is False


# ---------------------------------------------------------------------------
# Sync/async parity smoke (read-after-commit through the same connection)
# ---------------------------------------------------------------------------


async def test_async_writer_reads_back_through_async_session(
    async_db: async_sessionmaker,
) -> None:
    """End-to-end: an async write is visible via an async read in the same test.

    This is the smallest possible cross-call check and validates that
    the per-test ``AsyncConnection`` rebinding in the ``async_db``
    fixture (``tests/conftest.py``) actually plumbs through to the
    store's ``AsyncSessionLocal()`` calls.
    """
    store = IdempotencyStore()
    assert await store.try_mark_seen_async("smoke-1") is True
    assert await store.has_seen_async("smoke-1") is True


@pytest.mark.parametrize("count", [1, 50])
async def test_async_bulk_inserts(
    async_db: async_sessionmaker,
    count: int,
) -> None:
    """Bulk async inserts all visible without pruning kicking in."""
    store = IdempotencyStore()
    for i in range(count):
        assert await store.try_mark_seen_async(f"bulk-{count}-{i}") is True
    for i in range(count):
        assert await store.has_seen_async(f"bulk-{count}-{i}") is True
