"""Tests for the async API of ``UserStore`` (issue #1151).

Mirrors the sync ``UserStore`` surface for the ``*_async`` peers added
in the dual-API rollout. All tests opt into the per-test ``async_db``
fixture (see ``tests/conftest.py``) so writes are rolled back at
teardown. Follows the IdempotencyStore pilot pattern from #1199.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.user_db import UserStore
from backend.app.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker) -> int:
    """Return the total number of User rows in the DB."""
    async with factory() as db:
        return (await db.scalar(select(func.count(User.id)))) or 0


# ---------------------------------------------------------------------------
# get_by_id_async
# ---------------------------------------------------------------------------


async def test_async_get_by_id_returns_user(
    async_db: async_sessionmaker,
) -> None:
    """``get_by_id_async`` returns a DTO for an existing user."""
    store = UserStore()
    created = await store.create_async("google_get_id_1", phone="+15555550001")

    fetched = await store.get_by_id_async(created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == "google_get_id_1"
    assert fetched.phone == "+15555550001"


async def test_async_get_by_id_returns_none_for_missing(
    async_db: async_sessionmaker,
) -> None:
    """``get_by_id_async`` returns ``None`` for an unknown id."""
    store = UserStore()
    assert await store.get_by_id_async("does-not-exist") is None


async def test_async_get_by_id_accepts_int_like_sync(
    async_db: async_sessionmaker,
) -> None:
    """``get_by_id_async`` accepts ``int`` ids and stringifies them, matching sync."""
    store = UserStore()
    # The User.id PK is a UUID string in practice, so a missing int
    # lookup should round-trip to None without raising.
    assert await store.get_by_id_async(0) is None


# ---------------------------------------------------------------------------
# get_by_user_id_async
# ---------------------------------------------------------------------------


async def test_async_get_by_user_id_returns_user(
    async_db: async_sessionmaker,
) -> None:
    """``get_by_user_id_async`` looks up by the unique ``user_id`` column."""
    store = UserStore()
    created = await store.create_async("google_lookup_1")

    fetched = await store.get_by_user_id_async("google_lookup_1")

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == "google_lookup_1"


async def test_async_get_by_user_id_returns_none_for_missing(
    async_db: async_sessionmaker,
) -> None:
    """``get_by_user_id_async`` returns ``None`` for an unknown user_id."""
    store = UserStore()
    assert await store.get_by_user_id_async("nope") is None


# ---------------------------------------------------------------------------
# create_async
# ---------------------------------------------------------------------------


async def test_async_create_inserts_row(
    async_db: async_sessionmaker,
) -> None:
    """``create_async`` inserts a row and returns the populated DTO."""
    store = UserStore()
    before = await _row_count(async_db)

    created = await store.create_async(
        "google_create_1",
        phone="+15555550100",
        timezone="America/Los_Angeles",
    )

    assert created.user_id == "google_create_1"
    assert created.phone == "+15555550100"
    assert created.timezone == "America/Los_Angeles"
    assert created.id  # populated by the DB / model default
    assert await _row_count(async_db) == before + 1


async def test_async_create_then_lookup_round_trip(
    async_db: async_sessionmaker,
) -> None:
    """An async create is visible to an async lookup in the same test."""
    store = UserStore()
    created = await store.create_async("google_round_trip")

    by_id = await store.get_by_id_async(created.id)
    by_user_id = await store.get_by_user_id_async("google_round_trip")

    assert by_id is not None and by_id.id == created.id
    assert by_user_id is not None and by_user_id.id == created.id


# ---------------------------------------------------------------------------
# update_async
# ---------------------------------------------------------------------------


async def test_async_update_changes_allowed_fields(
    async_db: async_sessionmaker,
) -> None:
    """``update_async`` writes through allowlisted fields."""
    store = UserStore()
    created = await store.create_async("google_update_1")

    updated = await store.update_async(
        created.id,
        phone="+15555550200",
        timezone="UTC",
        onboarding_complete=True,
    )

    assert updated is not None
    assert updated.phone == "+15555550200"
    assert updated.timezone == "UTC"
    assert updated.onboarding_complete is True

    # Re-read confirms the write is persisted (not just on the returned DTO).
    fetched = await store.get_by_id_async(created.id)
    assert fetched is not None
    assert fetched.phone == "+15555550200"
    assert fetched.timezone == "UTC"
    assert fetched.onboarding_complete is True


async def test_async_update_ignores_disallowed_fields(
    async_db: async_sessionmaker,
) -> None:
    """``update_async`` silently drops fields outside the allowlist (matches sync).

    ``id`` is not in ``_USER_UPDATABLE_FIELDS``; the call should
    succeed but the PK should not change. (We can't pass ``user_id``
    here because the first positional parameter on ``update_async`` is
    also named ``user_id`` (the PK) and would collide.)
    """
    store = UserStore()
    created = await store.create_async("google_update_2", phone="+15555550300")

    updated = await store.update_async(
        created.id,
        id="should-not-change-either",
        phone="+15555550301",
    )

    assert updated is not None
    assert updated.user_id == "google_update_2"
    assert updated.id == created.id
    assert updated.phone == "+15555550301"


async def test_async_update_returns_none_for_missing(
    async_db: async_sessionmaker,
) -> None:
    """``update_async`` returns ``None`` when the user does not exist."""
    store = UserStore()
    assert await store.update_async("does-not-exist", phone="+15555559999") is None


# ---------------------------------------------------------------------------
# list_all_async
# ---------------------------------------------------------------------------


async def test_async_list_all_returns_users_in_creation_order(
    async_db: async_sessionmaker,
) -> None:
    """``list_all_async`` returns users ordered by ``created_at`` ascending."""
    store = UserStore()
    a = await store.create_async("google_list_a")
    b = await store.create_async("google_list_b")
    c = await store.create_async("google_list_c")

    rows = await store.list_all_async()

    ids = [u.id for u in rows]
    assert {a.id, b.id, c.id}.issubset(set(ids))
    # Relative order is preserved among the three we just created.
    pos = {uid: i for i, uid in enumerate(ids)}
    assert pos[a.id] < pos[b.id] < pos[c.id]


async def test_async_list_all_empty(
    async_db: async_sessionmaker,
) -> None:
    """``list_all_async`` returns an empty list when no users exist."""
    store = UserStore()
    assert await store.list_all_async() == []


# ---------------------------------------------------------------------------
# Per-test isolation canary (mirrors test_idempotency_pruning_async.py)
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
) -> None:
    """Half 1 of a paired check that the async fixture rolls back between tests.

    Writes a user; the paired test below must not see it.
    """
    store = UserStore()
    await store.create_async("google_iso_canary")
    assert await store.get_by_user_id_async("google_iso_canary") is not None


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = UserStore()
    assert await store.get_by_user_id_async("google_iso_canary") is None


# ---------------------------------------------------------------------------
# Sync/async parity: each pair shares its builders, so the same query
# should agree across APIs in the same test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lookup_field",
    ["id", "user_id"],
)
async def test_async_writer_visible_via_async_lookup(
    async_db: async_sessionmaker,
    lookup_field: str,
) -> None:
    """An async create is visible via the matching async getter."""
    store = UserStore()
    created = await store.create_async(f"google_parity_{lookup_field}")

    if lookup_field == "id":
        fetched = await store.get_by_id_async(created.id)
    else:
        fetched = await store.get_by_user_id_async(created.user_id)

    assert fetched is not None
    assert fetched.id == created.id
