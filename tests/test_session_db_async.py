"""Tests for the async API of ``SessionStore`` (issue #1152).

Mirrors the pilot ``tests/test_idempotency_pruning_async.py`` (PR #1199)
for the ``*_async`` peers added in the SessionStore conversion. All
tests opt into the per-test ``async_db`` fixture (see
``tests/conftest.py``) so writes are rolled back at teardown. Each
test_user is created via the sync ``test_user`` fixture and then read
by the async API; the ``test_user`` fixture runs through the sync
isolation transaction, so we re-create the user row inside the async
transaction for tests that need it visible to the async session.

Coverage:

  * Sync/async parity for every public method.
  * Iso-canary pair to confirm the async fixture rolls back between tests.
  * ``pg_advisory_xact_lock`` concurrency regression for the
    ``get_or_create_session_async`` path. Mirrors
    ``tests/test_approval.py::TestApprovalLockSerialization`` (PR #1198).
    Same matrix: same-key serializes, different-key parallel.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from backend.app.agent.session_db import (
    SessionStore,
    _advisory_lock_key,
    _advisory_lock_sql,
)
from backend.app.models import ChatSession, Message, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user(factory: async_sessionmaker, user_pk: str | None = None) -> str:
    """Insert a User row inside the async transaction and return its ``id`` PK.

    The sync ``test_user`` fixture writes through the sync per-test
    transaction, which is invisible to the async session under READ
    COMMITTED. Async tests that need a User row should call this helper
    instead so the row lives on the async connection.
    """
    pk = user_pk or str(uuid.uuid4())
    # Build a digits-only phone suffix; explicit user_pk values like
    # "iso-canary-user" produce non-numeric slices that violate phone format.
    phone_suffix = "".join(ch for ch in pk if ch.isdigit())[:6].ljust(6, "0")
    async with factory() as db:
        db.add(
            User(
                id=pk,
                user_id=f"async-user-{pk[:8]}",
                phone=f"+15555{phone_suffix}",
                channel_identifier=f"chan-{pk[:8]}",
                preferred_channel="telegram",
                onboarding_complete=True,
            )
        )
        await db.commit()
    return pk


# ---------------------------------------------------------------------------
# load_session / list_sessions parity
# ---------------------------------------------------------------------------


async def test_load_session_async_returns_none_for_missing(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    assert await store.load_session_async("does-not-exist") is None


async def test_load_session_async_round_trip(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, is_new = await store.get_or_create_session_async()
    assert is_new is True

    loaded = await store.load_session_async(session.session_id)
    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.user_id == user_id


async def test_list_sessions_async_returns_user_sessions(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    await store.get_or_create_session_async()

    sessions = await store.list_sessions_async()
    assert len(sessions) == 1
    assert sessions[0].user_id == user_id


# ---------------------------------------------------------------------------
# get_or_create_session_async (advisory-lock site)
# ---------------------------------------------------------------------------


async def test_get_or_create_session_async_creates_then_returns(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)

    first, is_new_1 = await store.get_or_create_session_async()
    assert is_new_1 is True

    second, is_new_2 = await store.get_or_create_session_async()
    assert is_new_2 is False
    assert second.session_id == first.session_id


async def test_get_or_create_session_async_advances_last_message_at(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    first, _ = await store.get_or_create_session_async()
    second, _ = await store.get_or_create_session_async()
    # last_message_at refreshed on the second call (>= because clock granularity).
    assert second.last_message_at >= first.last_message_at


# ---------------------------------------------------------------------------
# add_message_async / add_message_by_session_id_async
# ---------------------------------------------------------------------------


async def test_add_message_async_inserts_and_assigns_seq(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()

    first = await store.add_message_async(session, direction="inbound", body="hi")
    second = await store.add_message_async(session, direction="outbound", body="hello")
    assert first.seq == 1
    assert second.seq == 2
    assert session.messages[-1].body == "hello"


async def test_add_message_async_round_trips_thinking_text(
    async_db: async_sessionmaker,
) -> None:
    """Outbound messages persist and re-read the LLM's extended-thinking text.

    Guards three things at once: the new ``thinking_text`` column is
    written by ``add_message_async``, the ``EncryptedString`` decorator
    round-trips the value (a non-envelope value would raise on read), and
    the in-memory ``StoredMessage`` carries the same field so callers do
    not have to re-fetch.
    """
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()

    saved = await store.add_message_async(
        session,
        direction="outbound",
        body="here is the answer",
        thinking_text="step 1: parse the ask\nstep 2: pick the tool\nstep 3: phrase the reply",
    )
    assert saved.thinking_text == (
        "step 1: parse the ask\nstep 2: pick the tool\nstep 3: phrase the reply"
    )

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert reloaded.messages[-1].thinking_text == (
        "step 1: parse the ask\nstep 2: pick the tool\nstep 3: phrase the reply"
    )


async def test_add_message_by_session_id_async_assigns_seq_independently(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()

    first = await store.add_message_by_session_id_async(
        session.session_id, direction="inbound", body="hi"
    )
    second = await store.add_message_by_session_id_async(
        session.session_id, direction="outbound", body="hello"
    )
    assert first.seq == 1
    assert second.seq == 2


async def test_add_message_by_session_id_async_raises_on_unknown(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    with pytest.raises(ValueError):
        await store.add_message_by_session_id_async(
            "unknown-session", direction="inbound", body="hi"
        )


# ---------------------------------------------------------------------------
# update_message_async / update_initial_system_prompt_async
# ---------------------------------------------------------------------------


async def test_update_message_async_changes_body(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    msg = await store.add_message_async(session, direction="inbound", body="old")

    await store.update_message_async(session, msg.seq, body="new")

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert reloaded.messages[0].body == "new"
    # In-memory DTO also updated.
    assert session.messages[0].body == "new"


async def test_update_message_async_ignores_unknown_field(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    msg = await store.add_message_async(session, direction="inbound", body="keep")

    # Unknown field silently ignored (matches sync behavior).
    await store.update_message_async(session, msg.seq, not_a_field="x", body="ok")

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert reloaded.messages[0].body == "ok"


async def test_update_initial_system_prompt_async_writes_once(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()

    await store.update_initial_system_prompt_async(session, "first prompt")
    # Calling again is a no-op because the DTO already has it set.
    await store.update_initial_system_prompt_async(session, "second prompt")

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert reloaded.initial_system_prompt == "first prompt"


# ---------------------------------------------------------------------------
# delete_*_async
# ---------------------------------------------------------------------------


async def test_delete_message_async_removes_one(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    await store.add_message_async(session, direction="inbound", body="a")
    await store.add_message_async(session, direction="inbound", body="b")

    assert await store.delete_message_async(session.session_id, 1) is True
    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert [m.body for m in reloaded.messages] == ["b"]


async def test_delete_message_async_returns_false_when_missing(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    assert await store.delete_message_async(session.session_id, 99) is False


async def test_delete_messages_by_seqs_async_removes_subset(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    for body in ("a", "b", "c", "d"):
        await store.add_message_async(session, direction="inbound", body=body)

    deleted = await store.delete_messages_by_seqs_async(session.session_id, [1, 3])
    assert deleted == 2

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert sorted(m.body for m in reloaded.messages) == ["b", "d"]


async def test_delete_messages_async_clears_history_and_prompt(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    await store.add_message_async(session, direction="inbound", body="a")
    await store.update_initial_system_prompt_async(session, "prompt")

    deleted = await store.delete_messages_async(session.session_id)
    assert deleted == 1

    reloaded = await store.load_session_async(session.session_id)
    assert reloaded is not None
    assert reloaded.messages == []
    assert reloaded.initial_system_prompt == ""


async def test_delete_messages_async_resets_trim_watermark(
    async_db: async_sessionmaker,
) -> None:
    """Clearing the conversation must reset ``last_trim_seq`` to None.

    Regression for the dev-environment "agent loses all context on every
    turn" report: after a clear, the next inserted message gets ``seq=1``
    (because ``_select_max_seq`` returns 0 on an empty table). If
    ``last_trim_seq`` is left at its pre-clear value (say 199),
    ``load_conversation_history`` filters every new message out with
    ``seq > last_trim_seq`` and the LLM only ever sees the live inbound.
    """
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    await store.add_message_async(session, direction="inbound", body="a")

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        cs.last_trim_seq = 199
        await db.commit()

    await store.delete_messages_async(session.session_id)

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        assert cs.last_trim_seq is None


async def test_delete_message_async_resets_orphaned_trim_watermark(
    async_db: async_sessionmaker,
) -> None:
    """Per-seq delete that orphans the watermark must reset it.

    Production repro: a session was compacted up to seq=335 (so
    ``last_trim_seq=335``), then the user deleted every remaining row
    via the per-seq endpoint. Future inserts started at seq=1 again
    (``_select_max_seq`` returns 0 on an empty table), but
    ``load_conversation_history`` kept filtering them all out because
    ``seq > 335`` was never true. The LLM saw only the live inbound,
    losing every prior turn, including freshly attached media.
    """
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    await store.add_message_async(session, direction="inbound", body="a")

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        cs.last_trim_seq = 199
        await db.commit()

    assert await store.delete_message_async(session.session_id, 1) is True

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        assert cs.last_trim_seq is None


async def test_delete_message_async_preserves_live_trim_watermark(
    async_db: async_sessionmaker,
) -> None:
    """Per-seq delete that leaves rows above the watermark must keep it.

    Trimmed facts already live in MEMORY.md / USER.md / SOUL.md;
    re-feeding pre-watermark rows to the LLM would duplicate the
    extracted memory. Only clear the watermark when the delete leaves
    nothing above it.
    """
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    for body in ("a", "b", "c"):
        await store.add_message_async(session, direction="inbound", body=body)

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        cs.last_trim_seq = 1
        await db.commit()

    assert await store.delete_message_async(session.session_id, 2) is True

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        assert cs.last_trim_seq == 1


async def test_delete_messages_by_seqs_async_resets_orphaned_trim_watermark(
    async_db: async_sessionmaker,
) -> None:
    """Batch-seq delete that orphans the watermark must reset it.

    Same failure mode as the per-seq path: deleting every live row in a
    single batch call leaves an orphan watermark that silently filters
    every future insert.
    """
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    for body in ("a", "b"):
        await store.add_message_async(session, direction="inbound", body=body)

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        cs.last_trim_seq = 50
        await db.commit()

    deleted = await store.delete_messages_by_seqs_async(session.session_id, [1, 2])
    assert deleted == 2

    async with async_db() as db:
        cs = (
            await db.execute(select(ChatSession).where(ChatSession.user_id == user_id))
        ).scalar_one()
        assert cs.last_trim_seq is None


# ---------------------------------------------------------------------------
# last-timestamp helpers
# ---------------------------------------------------------------------------


async def test_last_timestamp_async_returns_max_per_direction(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()

    inbound = await store.add_message_async(session, direction="inbound", body="in")
    outbound = await store.add_message_async(session, direction="outbound", body="out")

    in_ts = await store.get_last_inbound_timestamp_async()
    out_ts = await store.get_last_outbound_timestamp_async()
    assert in_ts is not None
    assert out_ts is not None
    # Async write timestamps are stored UTC; just check both come back.
    assert in_ts.isoformat() == inbound.timestamp
    assert out_ts.isoformat() == outbound.timestamp


# ---------------------------------------------------------------------------
# recent / other-session collectors
# ---------------------------------------------------------------------------


async def test_get_recent_messages_async_returns_chronological(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    for body in ("first", "second", "third"):
        await store.add_message_async(session, direction="inbound", body=body)

    recent = await store.get_recent_messages_async(count=10)
    assert [m.body for m in recent] == ["first", "second", "third"]


async def test_get_other_session_messages_async_excludes_named(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _create_user(async_db)
    store = SessionStore(user_id)
    session, _ = await store.get_or_create_session_async()
    await store.add_message_async(session, direction="inbound", body="hidden")

    excluded = await store.get_other_session_messages_async(
        exclude_session_id=session.session_id, count=10
    )
    assert excluded == []


# ---------------------------------------------------------------------------
# Iso-canary pair
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
) -> None:
    """Half 1: writes a session row that the paired test must not see."""
    user_id = await _create_user(async_db, user_pk="iso-canary-user-id")
    store = SessionStore(user_id)
    await store.get_or_create_session_async()
    async with async_db() as db:
        rows = (await db.execute(select(ChatSession).filter_by(user_id=user_id))).scalars().all()
        assert len(rows) == 1


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
) -> None:
    """Half 2: confirms the previous test's writes were rolled back."""
    async with async_db() as db:
        rows = (
            (await db.execute(select(ChatSession).filter_by(user_id="iso-canary-user-id")))
            .scalars()
            .all()
        )
        assert rows == []
        msg_rows = (await db.execute(select(Message))).scalars().all()
        assert msg_rows == []


# ---------------------------------------------------------------------------
# Advisory-lock concurrency regression
# ---------------------------------------------------------------------------


class TestSessionAdvisoryLockSerialization:
    """Async equivalent of ``tests/test_approval.py::TestApprovalLockSerialization``.

    Verifies that the ``pg_advisory_xact_lock`` taken by
    ``get_or_create_session_async`` (and the pure builder
    ``_advisory_lock_sql``) actually serializes concurrent same-user
    callers and does NOT serialize different-user callers. Mirrors the
    sync ApprovalStore tests added in PR #1198, ported to ``asyncio``.

    Concurrency primitive: ``asyncio.gather`` with two ``asyncio.Event``
    handshakes per task. Each task opens its own ``AsyncSession`` over a
    fresh connection from the per-test ``_pg_async_engine`` so they
    contend on the real Postgres lock rather than serializing on a
    shared connection. The tasks only acquire the lock and commit; no
    INSERT/UPDATE leaks past the task's transaction.

    These tests do NOT use the ``async_db`` fixture: ``async_db`` rebinds
    the singleton factory to a single shared connection so that test
    writes can be rolled back, which would defeat the parallelism check
    here. Instead each task opens its own connection from
    ``_pg_async_engine`` and runs in its own self-contained transaction
    that it commits before the test ends, leaving no rows behind because
    no rows were inserted.
    """

    # Keep the same tolerances as the sync sibling so flake-rate stays
    # comparable across the two implementations.
    _HOLD_S = 0.4
    _ACQUIRE_TIMEOUT_S = 5.0

    async def _acquire_in_task(
        self,
        engine: AsyncEngine,
        user_id: str,
        ready: asyncio.Event,
        release: asyncio.Event,
        result: dict[str, float],
        label: str,
    ) -> None:
        """Open a fresh AsyncSession, take the lock, wait, then commit.

        Mirrors ``_acquire_in_thread`` from the sync test. The session is
        bound to a connection checked out directly from the function-scoped
        async engine so it is isolated from all other tasks. Records the
        acquire / commit monotonic timestamps so the test can assert
        ordering relative to ``_HOLD_S``.
        """
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await db.execute(
                _advisory_lock_sql(),
                {"k": _advisory_lock_key(user_id)},
            )
            result[f"{label}_acquired"] = time.monotonic()
            ready.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(release.wait(), timeout=self._ACQUIRE_TIMEOUT_S)
            await db.commit()
            result[f"{label}_committed"] = time.monotonic()

    async def test_same_user_lock_serializes_concurrent_writers(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """Two tasks acquiring the lock for the same user must run strictly
        one at a time. The second task must not acquire until the first
        commits."""
        user_id = "session-lock-serial-user"
        a_ready = asyncio.Event()
        a_release = asyncio.Event()
        b_ready = asyncio.Event()
        b_release = asyncio.Event()
        results: dict[str, float] = {}

        task_a = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_id, a_ready, a_release, results, "a")
        )

        # Wait for A to actually hold the lock before starting B, so the
        # ordering is deterministic regardless of asyncio scheduling.
        await asyncio.wait_for(a_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        task_b = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_id, b_ready, b_release, results, "b")
        )

        # While A still holds the lock, B must NOT acquire it. Use a
        # bounded wait that is much longer than any reasonable acquisition
        # path through Postgres so a slow runner does not falsely pass.
        try:
            await asyncio.wait_for(b_ready.wait(), timeout=self._HOLD_S)
            b_acquired_during_a = True
        except TimeoutError:
            b_acquired_during_a = False
        assert not b_acquired_during_a, (
            "task B acquired the lock before task A released it; "
            "pg_advisory_xact_lock did not serialize same-user writers"
        )

        # Release A; B should now proceed.
        a_release.set()
        await asyncio.wait_for(task_a, timeout=self._ACQUIRE_TIMEOUT_S)

        await asyncio.wait_for(b_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        b_release.set()
        await asyncio.wait_for(task_b, timeout=self._ACQUIRE_TIMEOUT_S)

    async def test_different_users_do_not_contend(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """A second task holding the lock for a DIFFERENT user must run in
        parallel with the first. Different lock keys do not contend."""
        user_a = "session-lock-parallel-user-a"
        user_c = "session-lock-parallel-user-c"

        a_ready = asyncio.Event()
        a_release = asyncio.Event()
        c_ready = asyncio.Event()
        c_release = asyncio.Event()
        results: dict[str, float] = {}

        task_a = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_a, a_ready, a_release, results, "a")
        )
        await asyncio.wait_for(a_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        # Start C while A is still holding its lock for user_a. Because
        # user_c hashes to a different advisory-lock key, C must acquire
        # immediately rather than waiting on A.
        task_c = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_c, c_ready, c_release, results, "c")
        )
        await asyncio.wait_for(c_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        # C acquired while A was still in its critical section.
        assert "a_committed" not in results, (
            "task A committed before C acquired; the parallelism check "
            "did not actually exercise overlapping critical sections"
        )

        # Tear down in either order.
        c_release.set()
        await asyncio.wait_for(task_c, timeout=self._ACQUIRE_TIMEOUT_S)
        a_release.set()
        await asyncio.wait_for(task_a, timeout=self._ACQUIRE_TIMEOUT_S)
