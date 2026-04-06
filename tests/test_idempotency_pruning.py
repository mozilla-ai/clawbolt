"""Tests for IdempotencyStore pruning of old entries beyond _SEEN_MAX."""

from __future__ import annotations

from unittest.mock import patch

from backend.app.agent.stores import IdempotencyStore
from backend.app.database import SessionLocal
from backend.app.models import IdempotencyKey

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_count() -> int:
    """Return the total number of IdempotencyKey rows in the database."""
    db = SessionLocal()
    try:
        return db.query(IdempotencyKey).count()
    finally:
        db.close()


def _surviving_ids() -> set[str]:
    """Return the set of external_id values still present in the table."""
    db = SessionLocal()
    try:
        return {row.external_id for row in db.query(IdempotencyKey).all()}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prune_removes_oldest_keeps_newest() -> None:
    """_prune() deletes the oldest rows and keeps the newest up to the cap."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 5):
            assert store.try_mark_seen(f"ext-{i}") is True

    # The oldest 5 should have been pruned, newest 10 should survive.
    for i in range(5):
        assert not store.has_seen(f"ext-{i}"), f"ext-{i} should have been pruned"
    for i in range(5, small_max + 5):
        assert store.has_seen(f"ext-{i}"), f"ext-{i} should still exist"


def test_prune_deterministic_on_every_insert() -> None:
    """Pruning fires on every insert, enforcing a hard cap."""
    store = IdempotencyStore()
    small_max = 20

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max + 50):
            store.try_mark_seen(f"det-{i}")

    # Table must never exceed small_max after pruning runs.
    assert _row_count() == small_max

    # The surviving rows must be the most recent ones.
    surviving = _surviving_ids()
    for i in range(small_max + 50 - small_max, small_max + 50):
        assert f"det-{i}" in surviving, f"det-{i} should have survived"


def test_prune_noop_at_exact_max() -> None:
    """_prune() is a no-op when row count equals exactly _SEEN_MAX."""
    store = IdempotencyStore()
    small_max = 10

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        for i in range(small_max):
            store.try_mark_seen(f"exact-{i}")
        store._prune()

    assert _row_count() == small_max
    for i in range(small_max):
        assert store.has_seen(f"exact-{i}")


def test_prune_noop_when_below_max() -> None:
    """_prune() is a no-op when row count is below _SEEN_MAX."""
    store = IdempotencyStore()
    for i in range(5):
        store.try_mark_seen(f"below-{i}")

    store._prune()

    assert _row_count() == 5
    for i in range(5):
        assert store.has_seen(f"below-{i}")


def test_prune_on_empty_table() -> None:
    """_prune() on an empty table does not raise."""
    store = IdempotencyStore()
    store._prune()
    assert _row_count() == 0


def test_prune_with_seen_max_one() -> None:
    """_SEEN_MAX = 1 keeps only the latest row."""
    store = IdempotencyStore()

    with patch("backend.app.agent.stores._SEEN_MAX", 1):
        store.try_mark_seen("first")
        store.try_mark_seen("second")
        store.try_mark_seen("third")

    assert _row_count() == 1
    assert store.has_seen("third")
    assert not store.has_seen("first")
    assert not store.has_seen("second")


def test_duplicate_returns_false() -> None:
    """Duplicate external_id returns False."""
    store = IdempotencyStore()
    assert store.try_mark_seen("dup-1") is True
    assert store.try_mark_seen("dup-1") is False


def test_prune_exception_does_not_block_return() -> None:
    """If _prune() raises, try_mark_seen() still returns True and the key is persisted."""
    store = IdempotencyStore()

    with patch.object(store, "_prune", side_effect=RuntimeError("db exploded")):
        result = store.try_mark_seen("safe-1")

    assert result is True
    assert store.has_seen("safe-1")


def test_repeated_prune_does_not_over_delete() -> None:
    """Multiple _prune() calls never reduce the table below _SEEN_MAX."""
    store = IdempotencyStore()
    small_max = 5

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        # Insert without pruning to set up the table.
        with patch.object(store, "_prune"):
            for i in range(small_max + 3):
                store.try_mark_seen(f"conc-{i}")

        assert _row_count() == small_max + 3

        # Multiple sequential prunes must converge: first prune deletes
        # overflow, subsequent prunes are no-ops.
        store._prune()
        store._prune()
        store._prune()

    # Must not have over-deleted below small_max.
    assert _row_count() == small_max

    # The newest rows must survive.
    surviving = _surviving_ids()
    for i in range(3, small_max + 3):
        assert f"conc-{i}" in surviving


def test_prune_is_self_correcting_after_external_delete() -> None:
    """If rows disappear between COUNT and DELETE, prune still converges."""
    store = IdempotencyStore()
    small_max = 5

    with patch("backend.app.agent.stores._SEEN_MAX", small_max):
        # Insert enough rows to trigger overflow.
        with patch.object(store, "_prune"):
            for i in range(small_max + 6):
                store.try_mark_seen(f"ext-del-{i}")

        assert _row_count() == small_max + 6

        # Simulate another worker pruning some rows before our prune.
        from backend.app.database import SessionLocal as SL

        db = SL()
        try:
            oldest = db.query(IdempotencyKey).order_by(IdempotencyKey.id.asc()).limit(3).all()
            for row in oldest:
                db.delete(row)
            db.commit()
        finally:
            db.close()

        assert _row_count() == small_max + 3

        # Our prune should still leave exactly small_max, not fewer.
        store._prune()

    assert _row_count() == small_max
