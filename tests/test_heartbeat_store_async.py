"""Tests for the async API of ``HeartbeatStore`` (issue #1154).

Mirrors the sync ``HeartbeatStore`` surface for the ``*_async`` peers
added in the dual-API rollout. All tests opt into the per-test
``async_db`` fixture (see ``tests/conftest.py``) so writes are rolled
back at teardown. Follows the IdempotencyStore pilot pattern from
PR #1199.

User-row setup runs through the shared ``async_test_user`` fixture in
``tests/conftest.py``; that fixture routes the insert through the
async connection because the sync ``test_user`` fixture opens its own
per-test transaction on a separate connection and rows committed there
are invisible to the async store under READ COMMITTED.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.stores import HeartbeatStore
from backend.app.models import HeartbeatLog, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _row_count(factory: async_sessionmaker, user_id: str) -> int:
    """Return the number of HeartbeatLog rows for the given user."""
    async with factory() as db:
        return (
            await db.scalar(
                select(func.count(HeartbeatLog.id)).where(HeartbeatLog.user_id == user_id)
            )
        ) or 0


# ---------------------------------------------------------------------------
# read_heartbeat_md_async / write_heartbeat_md_async
# ---------------------------------------------------------------------------


async def test_async_read_heartbeat_md_empty_when_unset(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``read_heartbeat_md_async`` returns ``""`` when the user row has no text."""
    store = HeartbeatStore(async_test_user.id)
    assert await store.read_heartbeat_md_async() == ""


async def test_async_write_then_read_heartbeat_md_round_trip(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """An async write is visible via an async read in the same test."""
    store = HeartbeatStore(async_test_user.id)
    await store.write_heartbeat_md_async("- next: call back the customer")

    content = await store.read_heartbeat_md_async()
    assert "call back the customer" in content


async def test_async_write_heartbeat_md_overwrites(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_heartbeat_md_async`` fully replaces the existing text."""
    store = HeartbeatStore(async_test_user.id)
    await store.write_heartbeat_md_async("old")
    await store.write_heartbeat_md_async("new")

    assert await store.read_heartbeat_md_async() == "new"


async def test_async_write_heartbeat_md_no_user_is_noop(
    async_db: async_sessionmaker,
) -> None:
    """``write_heartbeat_md_async`` silently no-ops when the user row is missing."""
    store = HeartbeatStore("does-not-exist")
    await store.write_heartbeat_md_async("anything")
    # And the read just returns empty string.
    assert await store.read_heartbeat_md_async() == ""


# ---------------------------------------------------------------------------
# log_heartbeat_async
# ---------------------------------------------------------------------------


async def test_async_log_heartbeat_inserts_row(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``log_heartbeat_async`` inserts a HeartbeatLog row visible via an async read."""
    store = HeartbeatStore(async_test_user.id)
    before = await _row_count(async_db, async_test_user.id)

    await store.log_heartbeat_async(
        action_type="send",
        message_text="ping",
        channel="telegram",
        reasoning="just because",
        tasks="",
    )

    assert await _row_count(async_db, async_test_user.id) == before + 1


async def test_async_log_heartbeat_defaults_match_sync(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Default keyword arguments match the sync surface (``action_type='send'``)."""
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async()

    async with async_db() as db:
        rows = (
            (
                await db.execute(
                    select(HeartbeatLog).where(HeartbeatLog.user_id == async_test_user.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].action_type == "send"


# ---------------------------------------------------------------------------
# get_daily_count_async
# ---------------------------------------------------------------------------


async def test_async_get_daily_count_excludes_skip_and_cleanup(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_daily_count_async`` excludes ``skip`` and ``cleanup`` rows.

    Mirrors the sync method: only nudges that consumed budget count.
    """
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async(action_type="send")
    await store.log_heartbeat_async(action_type="send")
    await store.log_heartbeat_async(action_type="skip")
    await store.log_heartbeat_async(action_type="cleanup")

    assert await store.get_daily_count_async() == 2


async def test_async_get_daily_count_zero_when_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_daily_count_async`` returns 0 when no log rows exist for the user."""
    store = HeartbeatStore(async_test_user.id)
    assert await store.get_daily_count_async() == 0


# ---------------------------------------------------------------------------
# get_recent_logs_async
# ---------------------------------------------------------------------------


async def test_async_get_recent_logs_orders_by_created_at(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_recent_logs_async`` returns rows ordered by ``created_at`` ascending."""
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async(message_text="first")
    await store.log_heartbeat_async(message_text="second")
    await store.log_heartbeat_async(message_text="third")

    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    logs = await store.get_recent_logs_async(since)

    assert [log.message_text for log in logs] == ["first", "second", "third"]


async def test_async_get_recent_logs_filters_by_since(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``get_recent_logs_async`` skips rows older than ``since``."""
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async(message_text="present")

    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
    logs = await store.get_recent_logs_async(future)

    assert logs == []


async def test_async_get_recent_logs_returns_dtos_with_isoformat_timestamps(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """The async path returns ``HeartbeatLogEntry`` DTOs, not ORM rows."""
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async(message_text="dto check")

    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    logs = await store.get_recent_logs_async(since)

    assert len(logs) == 1
    # The DTO converts created_at to an ISO string.
    assert isinstance(logs[0].created_at, str)
    assert "T" in logs[0].created_at


# ---------------------------------------------------------------------------
# Sync/async parity: an async write should be readable via the async path
# ---------------------------------------------------------------------------


async def test_async_writer_visible_via_async_read(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_heartbeat_md_async`` is visible through ``read_heartbeat_md_async``."""
    store = HeartbeatStore(async_test_user.id)
    await store.write_heartbeat_md_async("- check the truck oil")
    assert "truck oil" in await store.read_heartbeat_md_async()


# ---------------------------------------------------------------------------
# Per-test isolation canary (mirrors test_idempotency_pruning_async.py)
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 1: write a row; the paired test below must not see it."""
    store = HeartbeatStore(async_test_user.id)
    await store.log_heartbeat_async(message_text="iso-canary")
    assert await store.get_daily_count_async() == 1


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = HeartbeatStore(async_test_user.id)
    assert await store.get_daily_count_async() == 0
