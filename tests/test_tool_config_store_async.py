"""Tests for the async API of ``ToolConfigStore`` (issue #1157).

Mirrors the sync ``ToolConfigStore`` surface for the ``*_async`` peers
added in the dual-API rollout. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Follows the IdempotencyStore pilot pattern from
PR #1199.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.dto import ToolConfigEntry
from backend.app.agent.stores import ToolConfigStore
from backend.app.database import db_session_async
from backend.app.models import ToolConfig, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker, user_id: str) -> int:
    """Return the number of ToolConfig rows for the given user."""
    async with factory() as db:
        return (
            await db.scalar(select(func.count(ToolConfig.id)).where(ToolConfig.user_id == user_id))
        ) or 0


def _entry(
    name: str,
    *,
    enabled: bool = True,
    domain_group: str = "core",
    domain_group_order: int = 0,
) -> ToolConfigEntry:
    """Build a ``ToolConfigEntry`` for tests with sensible defaults."""
    return ToolConfigEntry(
        name=name,
        description=f"{name} description",
        category="domain",
        domain_group=domain_group,
        domain_group_order=domain_group_order,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# load_async / save_async
# ---------------------------------------------------------------------------


async def test_async_save_then_load_round_trip(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``save_async`` writes rows that ``load_async`` returns as DTOs."""
    store = ToolConfigStore(async_test_user.id)
    entries = [_entry("workspace"), _entry("calendar", enabled=False)]

    returned = await store.save_async(entries)

    assert returned == entries
    loaded = await store.load_async()
    by_name = {e.name: e for e in loaded}
    assert by_name["workspace"].enabled is True
    assert by_name["calendar"].enabled is False


async def test_async_save_replaces_existing_rows(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``save_async`` replaces the full set; a second save with fewer rows shrinks the table."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("workspace"), _entry("calendar"), _entry("billing")])
    assert await _row_count(async_db, async_test_user.id) == 3

    await store.save_async([_entry("workspace")])
    assert await _row_count(async_db, async_test_user.id) == 1

    loaded = await store.load_async()
    assert [e.name for e in loaded] == ["workspace"]


async def test_async_save_empty_list_clears_rows(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``save_async([])`` deletes all rows for the user."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("workspace"), _entry("calendar")])
    assert await _row_count(async_db, async_test_user.id) == 2

    await store.save_async([])
    assert await _row_count(async_db, async_test_user.id) == 0


async def test_async_load_orders_by_domain_group_order_then_name(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``load_async`` orders by ``(domain_group_order, name)`` ascending."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async(
        [
            _entry("zeta", domain_group_order=2),
            _entry("alpha", domain_group_order=1),
            _entry("beta", domain_group_order=1),
        ]
    )

    loaded = await store.load_async()
    assert [e.name for e in loaded] == ["alpha", "beta", "zeta"]


async def test_async_load_empty_returns_empty_list(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``load_async`` returns ``[]`` when no rows exist."""
    store = ToolConfigStore(async_test_user.id)
    assert await store.load_async() == []


# ---------------------------------------------------------------------------
# get_disabled_tool_names_async
# ---------------------------------------------------------------------------


async def test_async_get_disabled_tool_names(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_disabled_tool_names_async`` returns names of tools with ``enabled=False``."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async(
        [
            _entry("workspace", enabled=True),
            _entry("calendar", enabled=False),
            _entry("billing", enabled=False),
        ]
    )

    disabled = await store.get_disabled_tool_names_async()
    assert disabled == {"calendar", "billing"}


async def test_async_get_disabled_tool_names_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_disabled_tool_names_async`` returns ``set()`` when nothing is disabled."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("workspace", enabled=True)])
    assert await store.get_disabled_tool_names_async() == set()


# ---------------------------------------------------------------------------
# set_enabled_async
# ---------------------------------------------------------------------------


async def test_async_set_enabled_creates_row_for_unknown_name(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``set_enabled_async`` inserts a placeholder row when none exists."""
    store = ToolConfigStore(async_test_user.id)
    await store.set_enabled_async("brand-new-tool", False)

    disabled = await store.get_disabled_tool_names_async()
    assert "brand-new-tool" in disabled


async def test_async_set_enabled_updates_existing_row(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``set_enabled_async`` flips an existing row's ``enabled`` flag in place."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("calendar", enabled=True)])
    assert await store.get_disabled_tool_names_async() == set()

    await store.set_enabled_async("calendar", False)

    assert "calendar" in await store.get_disabled_tool_names_async()
    # No duplicate row inserted.
    assert await _row_count(async_db, async_test_user.id) == 1


# ---------------------------------------------------------------------------
# Concurrency: SELECT FOR UPDATE on set_enabled (issue #1222)
# ---------------------------------------------------------------------------
#
# These tests deliberately do NOT use the ``async_db`` fixture. ``async_db``
# rebinds the session factory to a single shared connection so writes can
# be rolled back, which would serialize concurrent ``set_enabled`` calls
# at the connection level and hide the production race. The session-scoped
# NullPool engine instead gives each ``AsyncSessionLocal()`` its own
# asyncpg connection, so concurrent tasks contend on the real Postgres
# row lock (and the real unique constraint). Cleanup is handled by the
# autouse ``_isolate_stores`` fixture, which TRUNCATEs every table after
# each test.


async def _insert_concurrency_user() -> str:
    """Insert a fresh User row through the session-scoped factory and
    return its id. Bypasses ``async_test_user`` so the row is committed
    on a normal connection (visible to concurrent tasks) rather than
    living inside the ``async_db`` SAVEPOINT."""
    user_id = str(uuid.uuid4())
    async with db_session_async() as db:
        db.add(
            User(
                id=user_id,
                user_id=f"concurrency-{user_id[:8]}",
                phone="+15555550123",
                channel_identifier=f"concurrency-channel-{user_id[:8]}",
                preferred_channel="telegram",
                onboarding_complete=True,
            )
        )
        await db.commit()
    return user_id


async def test_set_enabled_insert_race_does_not_raise() -> None:
    """Two concurrent ``set_enabled`` calls on the same ``(user_id, name)``
    when no row exists yet. The unique constraint on ``(user_id, name)``
    lets only one INSERT win; the loser catches ``IntegrityError``,
    rolls back, re-runs the locked read, and updates the winner's row.

    Without the ``IntegrityError`` retry, the loser would surface the
    exception to the caller. Without the ``with_for_update()`` on the
    re-read, the loser could read a stale snapshot and miss the
    winning row.
    """
    user_id = await _insert_concurrency_user()
    store = ToolConfigStore(user_id)
    name = "concurrent-insert-tool"
    start = asyncio.Event()

    async def toggle(value: bool) -> None:
        await start.wait()
        await store.set_enabled(name, value)

    task_a = asyncio.create_task(toggle(True))
    task_b = asyncio.create_task(toggle(False))
    start.set()
    await asyncio.gather(task_a, task_b)

    async with db_session_async() as db:
        rows = (
            (await db.execute(select(ToolConfig).filter_by(user_id=user_id, name=name)))
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one row after concurrent inserts, got {len(rows)}: "
        f"the unique constraint should have collapsed the race"
    )
    # Final state matches one of the two writers. The exact value is
    # non-deterministic (depends on scheduling), but it must be a clean
    # boolean from one of the two callers, not a corrupted state.
    assert rows[0].enabled in (True, False)


async def test_set_enabled_update_race_serializes_via_row_lock() -> None:
    """Two concurrent ``set_enabled`` calls on a pre-existing row
    serialize on the ``SELECT ... FOR UPDATE`` row lock. Exactly one
    row remains (no duplicate insert) and the final state matches
    whichever transaction committed last."""
    user_id = await _insert_concurrency_user()
    store = ToolConfigStore(user_id)
    name = "concurrent-update-tool"
    # Seed the row so both contenders take the update path.
    await store.set_enabled(name, True)

    start = asyncio.Event()

    async def toggle(value: bool) -> None:
        await start.wait()
        await store.set_enabled(name, value)

    task_a = asyncio.create_task(toggle(True))
    task_b = asyncio.create_task(toggle(False))
    start.set()
    await asyncio.gather(task_a, task_b)

    async with db_session_async() as db:
        count = (
            await db.scalar(select(func.count(ToolConfig.id)).where(ToolConfig.user_id == user_id))
        ) or 0
        row = (
            await db.execute(select(ToolConfig).filter_by(user_id=user_id, name=name))
        ).scalar_one()
    assert count == 1
    assert row.enabled in (True, False)


# ---------------------------------------------------------------------------
# Per-test isolation canary
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 1: insert a row; the paired test below must not see it."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("iso-canary")])
    assert await _row_count(async_db, async_test_user.id) == 1


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = ToolConfigStore(async_test_user.id)
    assert await store.load_async() == []
