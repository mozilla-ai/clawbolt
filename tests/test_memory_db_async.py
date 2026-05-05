"""Tests for the async API of ``MemoryStore`` (issue #1153).

Mirrors ``tests/test_memory.py`` for the ``*_async`` peers added in
the dual-API rollout. All tests opt into the per-test ``async_db``
fixture (see ``tests/conftest.py``) so writes are rolled back at
teardown. Follows the IdempotencyStore pilot pattern from PR #1199.

User-row setup runs through the shared ``async_test_user`` fixture in
``tests/conftest.py``; that fixture routes the insert through the
async connection because the sync ``test_user`` fixture opens its
own per-test transaction on a separate connection and rows committed
there are invisible to the async store under READ COMMITTED. This
matches the cross-API caveat called out in the design comment block
in ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.memory_db import MemoryStore, get_memory_store
from backend.app.models import MemoryDocument, User

# ---------------------------------------------------------------------------
# memory_text: read / write
# ---------------------------------------------------------------------------


async def test_async_write_and_read_memory(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_memory_async`` / ``read_memory_async`` round-trip content."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("## Pricing\n- Deck: $45/sqft")
    content = await store.read_memory_async()
    assert "Deck: $45/sqft" in content


async def test_async_read_memory_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``read_memory_async`` returns ``""`` when no MemoryDocument row exists."""
    store = MemoryStore(async_test_user.id)
    assert await store.read_memory_async() == ""


async def test_async_write_memory_overwrites(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_memory_async`` fully replaces the existing memory text."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("old content")
    await store.write_memory_async("new content")
    content = await store.read_memory_async()
    assert "new content" in content
    assert "old content" not in content


async def test_async_write_memory_normalises_trailing_newline(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Sync and async paths must persist the same trailing-newline form."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("body without newline")

    async with async_db() as db:
        row = (
            await db.execute(select(MemoryDocument).filter_by(user_id=async_test_user.id))
        ).scalar_one()
        assert row.memory_text == "body without newline\n"


# ---------------------------------------------------------------------------
# history_text: read / append
# ---------------------------------------------------------------------------


async def test_async_read_history_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``read_history_async`` returns ``""`` when no row exists."""
    store = MemoryStore(async_test_user.id)
    assert await store.read_history_async() == ""


async def test_async_append_history_creates_row(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``append_history_async`` creates the MemoryDocument row on first call."""
    store = MemoryStore(async_test_user.id)
    await store.append_history_async("first entry")

    history = await store.read_history_async()
    assert "first entry" in history


async def test_async_append_history_persists_first_entry(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """A first ``append_history_async`` call is readable via ``read_history_async``.

    ``MemoryDocument.history_text`` is an ``EncryptedString`` column,
    so the SQL-level ``history_text + suffix`` concatenation used by
    both sync and async paths operates on ciphertext rather than
    plaintext. Repeated appends do not produce a readable
    concatenated transcript today; that is a pre-existing limitation
    of the sync path which the async peer reproduces by design
    (dual-API parity per #1153). The fix belongs in a follow-up that
    rewrites the builder; both paths will pick it up via the shared
    ``_append_history_update`` helper.

    Contract this test pins down: the first append round-trips. The
    multi-append behavior is intentionally out of scope here so we
    do not lock in the broken contract.
    """
    store = MemoryStore(async_test_user.id)
    await store.append_history_async("first entry")
    assert "first entry" in await store.read_history_async()


async def test_async_append_history_does_not_disturb_memory_text(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Appending history must not clobber memory_text on the same row."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("memory body")
    await store.append_history_async("history line")

    assert await store.read_memory_async() == "memory body"
    assert await store.read_history_async() == "history line"


# ---------------------------------------------------------------------------
# soul_text and user_text on the User row
# ---------------------------------------------------------------------------


async def test_async_soul_round_trip_strips_header(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_soul_async`` writes a ``# Soul`` envelope; ``read_soul_async`` strips it."""
    store = MemoryStore(async_test_user.id)
    await store.write_soul_async("calm and curious")
    assert await store.read_soul_async() == "calm and curious"


async def test_async_user_round_trip_strips_header(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``write_user_async`` writes a ``# User`` envelope; ``read_user_async`` strips it."""
    store = MemoryStore(async_test_user.id)
    await store.write_user_async("prefers concise replies")
    assert await store.read_user_async() == "prefers concise replies"


async def test_async_read_soul_for_missing_user(
    async_db: async_sessionmaker,
) -> None:
    """``read_soul_async`` returns ``""`` for an unknown user_id."""
    store = MemoryStore("missing-user-id")
    assert await store.read_soul_async() == ""


async def test_async_read_user_for_missing_user(
    async_db: async_sessionmaker,
) -> None:
    """``read_user_async`` returns ``""`` for an unknown user_id."""
    store = MemoryStore("missing-user-id")
    assert await store.read_user_async() == ""


async def test_async_write_soul_noop_for_missing_user(
    async_db: async_sessionmaker,
) -> None:
    """Writes against a missing user must not raise (sync path swallows it)."""
    store = MemoryStore("missing-user-id")
    await store.write_soul_async("ignored")
    await store.write_user_async("ignored")


# ---------------------------------------------------------------------------
# Composite helpers
# ---------------------------------------------------------------------------


async def test_async_build_memory_context_with_memory(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``build_memory_context_async`` returns the memory text."""
    store = get_memory_store(async_test_user.id)
    await store.write_memory_async("## Pricing\n- Deck: $35/sqft")

    context = await store.build_memory_context_async()
    assert "$35/sqft" in context


async def test_async_build_memory_context_empty(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``build_memory_context_async`` returns ``""`` when no memory exists."""
    store = get_memory_store(async_test_user.id)
    assert await store.build_memory_context_async() == ""


# ---------------------------------------------------------------------------
# Iso-canary pair: prove the async fixture rolls back between tests.
# Mirrors ``test_idempotency_pruning_async`` (PR #1199). Uses a fixed
# user_id so the pair sees the same key without depending on the
# uuid-generating ``async_test_user`` fixture.
# ---------------------------------------------------------------------------

_ISO_USER_ID = "iso-canary-user"


async def _seed_iso_user(async_db: async_sessionmaker) -> None:
    """Insert the iso-canary User row through the async connection."""
    async with async_db() as db:
        db.add(
            User(
                id=_ISO_USER_ID,
                user_id="iso-canary",
                phone="+15555550199",
                channel_identifier="iso-canary-channel",
                preferred_channel="telegram",
                onboarding_complete=True,
            )
        )
        await db.commit()


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
) -> None:
    """Half 1: write a row; the paired test must not observe it."""
    await _seed_iso_user(async_db)
    store = MemoryStore(_ISO_USER_ID)
    await store.write_memory_async("iso-canary value")
    assert await store.read_memory_async() == "iso-canary value"


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
) -> None:
    """Half 2: confirm part A's write was rolled back.

    The User row from part A must be gone, and so must the
    MemoryDocument row attached to it. Both checks together confirm
    the outer transaction in the ``async_db`` fixture wound back the
    full state.
    """
    async with async_db() as db:
        user = (await db.execute(select(User).filter_by(id=_ISO_USER_ID))).scalar_one_or_none()
        assert user is None

    store = MemoryStore(_ISO_USER_ID)
    assert await store.read_memory_async() == ""


# ---------------------------------------------------------------------------
# Sync/async parity smoke: an async write is visible via an async read in
# the same test, going through the rebound ``AsyncSessionLocal()``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr_writer", "attr_reader"),
    [
        ("write_memory_async", "read_memory_async"),
        ("write_soul_async", "read_soul_async"),
        ("write_user_async", "read_user_async"),
    ],
)
async def test_async_writer_reads_back_through_async_session(
    async_db: async_sessionmaker,
    async_test_user: User,
    attr_writer: str,
    attr_reader: str,
) -> None:
    """End-to-end: async writes round-trip through an async read."""
    store = MemoryStore(async_test_user.id)
    writer = getattr(store, attr_writer)
    reader = getattr(store, attr_reader)
    await writer("smoke value")
    assert await reader() == "smoke value"
