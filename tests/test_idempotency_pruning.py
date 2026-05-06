"""Tests for IdempotencyStore pruning of old entries beyond _SEEN_MAX."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from backend.app.agent.stores import IdempotencyStore
from backend.app.database import db_session_async
from backend.app.models import IdempotencyKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count() -> int:
    """Return the total number of IdempotencyKey rows in the database."""
    async with db_session_async() as db:
        return len((await db.execute(select(IdempotencyKey))).scalars().all())


async def _surviving_ids() -> set[str]:
    """Return the set of external_id values still present in the table."""
    async with db_session_async() as db:
        rows = (await db.execute(select(IdempotencyKey))).scalars().all()
        return {row.external_id for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_prune_removes_oldest_keeps_newest() -> None:
    """_prune() deletes the oldest rows and keeps the newest up to the cap."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 5):
            assert await store.try_mark_seen(f"ext-{i}") is True

    # The oldest 5 should have been pruned, newest 10 should survive.
    for i in range(5):
        assert not await store.has_seen(f"ext-{i}"), f"ext-{i} should have been pruned"
    for i in range(5, small_max + 5):
        assert await store.has_seen(f"ext-{i}"), f"ext-{i} should still exist"


async def test_prune_deterministic_on_every_insert() -> None:
    """Pruning fires on every insert, enforcing a hard cap."""
    store = IdempotencyStore()
    small_max = 20

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 50):
            await store.try_mark_seen(f"det-{i}")

    # Table must never exceed small_max after pruning runs.
    assert await _row_count() == small_max

    # The surviving rows must be the most recent ones.
    surviving = await _surviving_ids()
    for i in range(small_max + 50 - small_max, small_max + 50):
        assert f"det-{i}" in surviving, f"det-{i} should have survived"


async def test_prune_noop_at_exact_max() -> None:
    """_prune() is a no-op when row count equals exactly _SEEN_MAX."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max):
            await store.try_mark_seen(f"exact-{i}")
        await store._prune()

    assert await _row_count() == small_max
    for i in range(small_max):
        assert await store.has_seen(f"exact-{i}")


async def test_prune_noop_when_below_max() -> None:
    """_prune() is a no-op when row count is below _SEEN_MAX."""
    store = IdempotencyStore()
    for i in range(5):
        await store.try_mark_seen(f"below-{i}")

    await store._prune()

    assert await _row_count() == 5
    for i in range(5):
        assert await store.has_seen(f"below-{i}")


async def test_prune_on_empty_table() -> None:
    """_prune() on an empty table does not raise."""
    store = IdempotencyStore()
    await store._prune()
    assert await _row_count() == 0


async def test_prune_with_seen_max_one() -> None:
    """_SEEN_MAX = 1 keeps only the latest row."""
    store = IdempotencyStore()

    with patch("backend.app.agent.stores._SEEN_MAX", 1):
        await store.try_mark_seen("first")
        await store.try_mark_seen("second")
        await store.try_mark_seen("third")

    assert await _row_count() == 1
    assert await store.has_seen("third")
    assert not await store.has_seen("first")
    assert not await store.has_seen("second")


async def test_duplicate_returns_false() -> None:
    """Duplicate external_id returns False."""
    store = IdempotencyStore()
    assert await store.try_mark_seen("dup-1") is True
    assert await store.try_mark_seen("dup-1") is False


async def test_prune_exception_does_not_block_return() -> None:
    """If _prune() raises, try_mark_seen() still returns True and the key is persisted."""
    store = IdempotencyStore()

    with patch.object(store, "_prune", AsyncMock(side_effect=RuntimeError("db exploded"))):
        result = await store.try_mark_seen("safe-1")

    assert result is True
    assert await store.has_seen("safe-1")


async def test_repeated_prune_does_not_over_delete() -> None:
    """Multiple _prune() calls never reduce the table below _SEEN_MAX."""
    store = IdempotencyStore()
    small_max = 5

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        # Insert without pruning to set up the table.
        with patch.object(store, "_prune", AsyncMock()):
            for i in range(small_max + 3):
                await store.try_mark_seen(f"conc-{i}")

        assert await _row_count() == small_max + 3

        # Multiple sequential prunes must converge: first prune deletes
        # overflow, subsequent prunes are no-ops.
        await store._prune()
        await store._prune()
        await store._prune()

    # Must not have over-deleted below small_max.
    assert await _row_count() == small_max

    # The newest rows must survive.
    surviving = await _surviving_ids()
    for i in range(3, small_max + 3):
        assert f"conc-{i}" in surviving


async def test_prune_is_self_correcting_after_external_delete() -> None:
    """If rows disappear between COUNT and DELETE, prune still converges."""
    store = IdempotencyStore()
    small_max = 5

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        # Insert enough rows to trigger overflow.
        with patch.object(store, "_prune", AsyncMock()):
            for i in range(small_max + 6):
                await store.try_mark_seen(f"ext-del-{i}")

        assert await _row_count() == small_max + 6

        # Simulate another worker pruning some rows before our prune.
        async with db_session_async() as db:
            oldest = (
                (
                    await db.execute(
                        select(IdempotencyKey).order_by(IdempotencyKey.id.asc()).limit(3)
                    )
                )
                .scalars()
                .all()
            )
            for row in oldest:
                await db.delete(row)
            await db.commit()

        assert await _row_count() == small_max + 3

        # Our prune should still leave exactly small_max, not fewer.
        await store._prune()

    assert await _row_count() == small_max
