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

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.agent.memory_db import MemoryStore, get_memory_store
from backend.app.models import MemoryDocument, User

# ---------------------------------------------------------------------------
# compare-and-swap writes (issue #1429)
# ---------------------------------------------------------------------------


async def test_async_write_memory_cas_match_writes(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """CAS write lands when the row still matches the caller's read."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("v1")
    current = await store.read_memory_async()
    assert await store.write_memory_async("v2", expected_current=current) is True
    assert "v2" in await store.read_memory_async()


async def test_async_write_memory_cas_mismatch_skips(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """A rewrite computed from a stale read must not clobber a newer write."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("v1")
    stale = await store.read_memory_async()
    # A concurrent writer (agent workspace tool, another compaction)
    # lands after the caller's read.
    await store.write_memory_async("agent fact")
    assert await store.write_memory_async("stale rewrite", expected_current=stale) is False
    assert await store.read_memory_async() == "agent fact"


async def test_async_write_memory_cas_empty_expected_matches_missing_row(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``expected_current=""`` matches a missing row (first compaction)."""
    store = MemoryStore(async_test_user.id)
    assert await store.write_memory_async("first", expected_current="") is True
    assert "first" in await store.read_memory_async()


async def test_async_write_user_cas_mismatch_skips(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    store = MemoryStore(async_test_user.id)
    await store.write_user_async("trade: deck builder")
    stale = await store.read_user_async()
    await store.write_user_async("trade: general contractor")
    assert await store.write_user_async("stale rewrite", expected_current=stale) is False
    assert await store.read_user_async() == "trade: general contractor"


async def test_async_write_soul_cas_mismatch_skips(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    store = MemoryStore(async_test_user.id)
    await store.write_soul_async("tone: friendly")
    stale = await store.read_soul_async()
    await store.write_soul_async("tone: blunt, no emojis")
    assert await store.write_soul_async("stale rewrite", expected_current=stale) is False
    assert await store.read_soul_async() == "tone: blunt, no emojis"


async def test_async_write_user_cas_match_writes(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    store = MemoryStore(async_test_user.id)
    await store.write_user_async("trade: deck builder")
    current = await store.read_user_async()
    assert await store.write_user_async("trade: remodeler", expected_current=current) is True
    assert await store.read_user_async() == "trade: remodeler"


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
    """``append_history`` creates the MemoryDocument row on first call."""
    store = MemoryStore(async_test_user.id)
    await store.append_history("first entry")

    history = await store.read_history_async()
    assert "first entry" in history


async def test_async_append_history_multi_append_round_trips(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Multiple sequential appends round-trip through ``read_history_async``.

    Regression test for the encrypted-history concat bug.
    ``MemoryDocument.history_text`` is an ``EncryptedString`` column,
    so the original SQL-level ``history_text + suffix`` builder
    concatenated ciphertext envelopes and broke decryption on read
    after the second append. The fix reads the row under
    ``SELECT ... FOR UPDATE``, concatenates plaintext in Python, and
    rewrites the column with a fresh envelope via the
    ``_append_history_update`` helper.
    """
    store = MemoryStore(async_test_user.id)
    await store.append_history("first entry")
    await store.append_history("second entry")
    await store.append_history("third entry")

    history = await store.read_history_async()
    # Each entry was appended with a trailing newline. ``read_history_async``
    # strips the outer whitespace, so the final entry's newline is gone but
    # the inter-entry newlines remain.
    assert history == "first entry\nsecond entry\nthird entry"


async def test_async_append_history_sequential_appends_after_seed(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Appending after a row already exists keeps every prior entry.

    Targets the second-and-later append path (``UPDATE`` branch), as
    opposed to the create-on-first-append branch covered by
    ``test_async_append_history_creates_row``. The pre-fix builder
    silently corrupted ``history_text`` here because SQL-side
    concatenation glued two ciphertext envelopes together.
    """
    store = MemoryStore(async_test_user.id)
    await store.append_history("seed")
    await store.append_history("follow-up")

    history = await store.read_history_async()
    assert history == "seed\nfollow-up"


async def test_async_append_history_does_not_disturb_memory_text(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """Appending history must not clobber memory_text on the same row."""
    store = MemoryStore(async_test_user.id)
    await store.write_memory_async("memory body")
    await store.append_history("history line")

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


# ---------------------------------------------------------------------------
# First-append concurrency: regression for issue #1224.
# ---------------------------------------------------------------------------


class TestMemoryStoreFirstAppendConcurrency:
    """Regression for issue #1224: ``MemoryStore.append_history`` first-append race.

    Before the fix, the create-on-first-append branch ran an
    unsynchronized SELECT then INSERT: ``SELECT ... FOR UPDATE`` on a
    missing row acquires no predicate lock, so two concurrent
    first-appends could both see ``None``, both INSERT, and the loser
    would hit ``IntegrityError`` on ``uq_memory_documents_user_id``.

    The fix takes a per-user ``pg_advisory_xact_lock`` at the top of
    ``append_history`` so the create branch is serialized. This test
    exercises the race directly: two tasks call ``append_history``
    against the same user with no row yet, gated through an
    ``asyncio.Barrier`` so both enter the critical section together.
    Both must succeed and the final history must contain both entries.

    These tests do NOT use the ``async_db`` fixture: ``async_db``
    rebinds the session factory to a single shared connection, which
    would serialize the two append calls inside Python and defeat the
    race. The session-scoped ``_isolate_async_engine`` autouse fixture
    already binds ``_async_session_factory`` to a ``NullPool`` async
    engine, so each ``db_session_async()`` opens its own asyncpg
    connection from the engine. The ``_isolate_stores`` autouse
    teardown TRUNCATEs every table after the test.
    """

    _ACQUIRE_TIMEOUT_S = 10.0

    async def _seed_user(self, engine: AsyncEngine, user_pk: str, phone: str) -> None:
        """Insert the User row through a dedicated connection.

        Commits immediately so concurrent tasks below can see the row
        through their own connections under READ COMMITTED.
        """
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add(
                User(
                    id=user_pk,
                    user_id=f"async-mem-{user_pk[:8]}",
                    phone=phone,
                    channel_identifier=f"mem-race-{user_pk[:8]}",
                    preferred_channel="telegram",
                    onboarding_complete=True,
                )
            )
            await db.commit()

    async def _append_in_task(
        self,
        user_pk: str,
        entry: str,
        barrier: asyncio.Barrier,
    ) -> str:
        """Wait at the barrier, then call ``append_history`` once.

        Both tasks release together so they hit the critical section
        in ``append_history`` concurrently. Each call goes through
        ``db_session_async()``, which under the session-scoped
        ``NullPool`` engine opens its own asyncpg connection.
        """
        await barrier.wait()
        store = MemoryStore(user_pk)
        return await store.append_history(entry)

    async def test_two_concurrent_first_appends_both_succeed(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """Two concurrent first-appends for the same user both land."""
        user_pk = "memstore-first-append-race-user"
        await self._seed_user(_pg_async_engine, user_pk, "+15555550144")

        barrier = asyncio.Barrier(2)
        task_a = asyncio.create_task(self._append_in_task(user_pk, "entry-A", barrier))
        task_b = asyncio.create_task(self._append_in_task(user_pk, "entry-B", barrier))

        # Both must complete without IntegrityError. ``asyncio.gather``
        # raises the first exception it sees, so a pre-fix run would
        # surface the race here.
        await asyncio.wait_for(
            asyncio.gather(task_a, task_b),
            timeout=self._ACQUIRE_TIMEOUT_S,
        )

        store = MemoryStore(user_pk)
        history = await store.read_history_async()
        assert "entry-A" in history, f"missing entry-A; history={history!r}"
        assert "entry-B" in history, f"missing entry-B; history={history!r}"
