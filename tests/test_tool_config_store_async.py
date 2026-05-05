"""Tests for the async API of ``ToolConfigStore`` (issue #1157).

Mirrors the sync ``ToolConfigStore`` surface for the ``*_async`` peers
added in the dual-API rollout. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Follows the IdempotencyStore pilot pattern from
PR #1199.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.dto import ToolConfigEntry
from backend.app.agent.stores import ToolConfigStore
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
    disabled_sub_tools: list[str] | None = None,
) -> ToolConfigEntry:
    """Build a ``ToolConfigEntry`` for tests with sensible defaults."""
    return ToolConfigEntry(
        name=name,
        description=f"{name} description",
        category="domain",
        domain_group=domain_group,
        domain_group_order=domain_group_order,
        enabled=enabled,
        disabled_sub_tools=disabled_sub_tools or [],
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


async def test_async_save_persists_disabled_sub_tools_as_json(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``save_async`` JSON-encodes ``disabled_sub_tools`` (matches sync)."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("workspace", disabled_sub_tools=["delete_file", "rename_file"])])

    async with async_db() as db:
        raw = (
            await db.execute(
                select(ToolConfig.disabled_sub_tools).where(
                    ToolConfig.user_id == async_test_user.id, ToolConfig.name == "workspace"
                )
            )
        ).scalar_one()
    assert json.loads(raw) == ["delete_file", "rename_file"]


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
# get_disabled_sub_tool_names_async
# ---------------------------------------------------------------------------


async def test_async_get_disabled_sub_tool_names_unions_across_groups(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_disabled_sub_tool_names_async`` unions sub-tool names across all groups."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async(
        [
            _entry("workspace", disabled_sub_tools=["delete_file", "rename_file"]),
            _entry("calendar", disabled_sub_tools=["delete_event"]),
            _entry("billing", disabled_sub_tools=[]),
        ]
    )

    disabled_subs = await store.get_disabled_sub_tool_names_async()
    assert disabled_subs == {"delete_file", "rename_file", "delete_event"}


async def test_async_get_disabled_sub_tool_names_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Returns ``set()`` when no group declares any disabled sub-tools."""
    store = ToolConfigStore(async_test_user.id)
    await store.save_async([_entry("workspace")])
    assert await store.get_disabled_sub_tool_names_async() == set()


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
