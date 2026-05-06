"""Tests for the orphan inbound startup recovery sweep.

The sweep finds inbound messages that landed in the DB but never produced
an outbound (worker died during the MessageBatcher window), and
re-dispatches each via ``_dispatch_to_pipeline``. These tests exercise:

* The orphan-detection SQL: an inbound with no following outbound is a
  candidate; an inbound followed by any outbound is not.
* The lookback window: messages older than the cutoff are ignored.
* The disable switch: ``inbound_recovery_lookback_minutes=0`` short-circuits.
* End-to-end dispatch: the recovery path calls ``_dispatch_to_pipeline``
  with the right arguments and counts successes correctly.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.agent.inbound_recovery import (
    _RECOVERY_LOCK_KEY,
    _build_dispatch_inputs,
    _find_orphaned_inbounds,
    _parse_media_refs,
    recover_orphan_inbound_messages,
)
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, Message, User
from tests.db_test_utils import open_test_db_session


async def _make_user_async(factory: async_sessionmaker) -> User:
    """Async peer of ``_make_user`` for end-to-end tests that exercise
    ``recover_orphan_inbound_messages`` (which now runs on the async
    DB engine; a sync write would land in a different connection and
    not be visible to the async sweep)."""
    async with factory() as db:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"recovery-test-{uuid.uuid4().hex[:8]}",
            phone="+15555550101",
            channel_identifier="+15555550101",
            preferred_channel="bluebubbles",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


async def _make_session_async(
    factory: async_sessionmaker, user: User, channel: str = "bluebubbles"
) -> ChatSession:
    """Async peer of ``_make_session``."""
    async with factory() as db:
        now = datetime.datetime.now(datetime.UTC)
        cs = ChatSession(
            session_id=f"sess-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            channel=channel,
            created_at=now,
            last_message_at=now,
        )
        db.add(cs)
        await db.commit()
        await db.refresh(cs)
        db.expunge(cs)
    return cs


async def _add_message_async(
    factory: async_sessionmaker,
    cs: ChatSession,
    direction: str,
    seq: int,
    body: str,
    *,
    minutes_ago: int = 0,
) -> Message:
    """Async peer of ``_add_message``."""
    async with factory() as db:
        ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
        msg = Message(
            session_id=cs.id,
            seq=seq,
            direction=direction,
            body=body,
            external_message_id=f"ext-{seq}",
            media_urls_json="[]",
            timestamp=ts,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        db.expunge(msg)
    return msg


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _find_orphaned_inbounds_with_async_db(
    cutoff_utc: datetime.datetime,
    freshness_floor_utc: datetime.datetime,
) -> list[tuple[Message, ChatSession]]:
    db = AsyncSessionLocal()
    try:
        return await _find_orphaned_inbounds(db, cutoff_utc, freshness_floor_utc)
    finally:
        await db.close()


async def _build_dispatch_inputs_with_async_db(
    msg: Message,
    chat_session: ChatSession,
) -> tuple[User, Any, Any] | None:
    db = AsyncSessionLocal()
    try:
        return await _build_dispatch_inputs(db, msg, chat_session)
    finally:
        await db.close()


def _try_acquire_lock_sync(connection: Any) -> bool:
    got_lock = connection.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:k))"),
        {"k": _RECOVERY_LOCK_KEY},
    ).scalar()
    connection.commit()
    return bool(got_lock)


def _release_lock_sync(connection: Any) -> None:
    connection.execute(
        text("SELECT pg_advisory_unlock(hashtext(:k))"),
        {"k": _RECOVERY_LOCK_KEY},
    )
    connection.commit()


def _make_user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"recovery-test-{uuid.uuid4().hex[:8]}",
        phone="+15555550101",
        channel_identifier="+15555550101",
        preferred_channel="bluebubbles",
        onboarding_complete=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.expunge(user)
    return user


def _make_session(db: Session, user: User, channel: str = "bluebubbles") -> ChatSession:
    now = datetime.datetime.now(datetime.UTC)
    cs = ChatSession(
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        channel=channel,
        created_at=now,
        last_message_at=now,
    )
    db.add(cs)
    db.commit()
    db.refresh(cs)
    db.expunge(cs)
    return cs


def _add_message(
    db: Session,
    cs: ChatSession,
    direction: str,
    seq: int,
    body: str,
    *,
    minutes_ago: int = 0,
) -> Message:
    ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes_ago)
    msg = Message(
        session_id=cs.id,
        seq=seq,
        direction=direction,
        body=body,
        external_message_id=f"ext-{seq}",
        media_urls_json="[]",
        timestamp=ts,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    db.expunge(msg)
    return msg


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def test_inbound_with_no_outbound_is_orphan() -> None:
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hello?", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _run(
            _find_orphaned_inbounds_with_async_db(cutoff, datetime.datetime.now(datetime.UTC))
        )
    finally:
        db.close()

    assert len(rows) == 1
    msg, found_cs = rows[0]
    assert msg.body == "hello?"
    assert found_cs.session_id == cs.session_id


def test_inbound_followed_by_outbound_is_not_orphan() -> None:
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hi", minutes_ago=2)
        _add_message(db, cs, MessageDirection.OUTBOUND, seq=2, body="hi back", minutes_ago=1)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _run(
            _find_orphaned_inbounds_with_async_db(cutoff, datetime.datetime.now(datetime.UTC))
        )
    finally:
        db.close()

    assert rows == []


def test_inbound_outside_lookback_is_ignored() -> None:
    """Messages older than the cutoff are excluded so we don't dispatch a
    stale reply for something the user has long since moved past."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="long ago", minutes_ago=120)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _run(
            _find_orphaned_inbounds_with_async_db(cutoff, datetime.datetime.now(datetime.UTC))
        )
    finally:
        db.close()

    assert rows == []


def test_orphan_detection_is_per_session() -> None:
    """A second session's outbound must not satisfy the first session's
    inbound. Without the per-session join the EXISTS would let one user's
    activity declare another user's inbound 'processed'."""
    db = open_test_db_session()
    try:
        user_a = _make_user(db)
        user_b = _make_user(db)
        cs_a = _make_session(db, user_a)
        cs_b = _make_session(db, user_b)

        _add_message(db, cs_a, MessageDirection.INBOUND, seq=1, body="A asks", minutes_ago=2)
        _add_message(db, cs_b, MessageDirection.OUTBOUND, seq=1, body="B reply", minutes_ago=1)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        rows = _run(
            _find_orphaned_inbounds_with_async_db(cutoff, datetime.datetime.now(datetime.UTC))
        )
    finally:
        db.close()

    # User A's inbound is still orphaned. User B has no inbound so nothing
    # to recover for them.
    assert len(rows) == 1
    msg, _ = rows[0]
    assert msg.body == "A asks"


# ---------------------------------------------------------------------------
# Dispatch input reconstruction
# ---------------------------------------------------------------------------


def test_build_dispatch_inputs_round_trips_message_fields() -> None:
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        msg = _add_message(
            db, cs, MessageDirection.INBOUND, seq=7, body="rebuild me", minutes_ago=5
        )

        result = _run(_build_dispatch_inputs_with_async_db(msg, cs))
    finally:
        db.close()

    assert result is not None
    rebuilt_user, state, stored = result
    assert rebuilt_user.id == user.id
    assert state.session_id == cs.session_id
    assert state.user_id == user.id
    assert stored.seq == 7
    assert stored.body == "rebuild me"
    assert stored.direction == MessageDirection.INBOUND


def test_build_dispatch_inputs_returns_none_when_user_missing() -> None:
    """Defense in depth: if the user row was deleted in the window between
    persist and recovery, log and skip rather than crash."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        msg = _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="x", minutes_ago=1)

        # Hard-delete user row so the rebuild lookup misses.
        db.delete(user)
        db.commit()

        result = _run(_build_dispatch_inputs_with_async_db(msg, cs))
    finally:
        db.close()

    assert result is None


# ---------------------------------------------------------------------------
# Media refs reconstruction
# ---------------------------------------------------------------------------


def test_parse_media_refs_handles_empty_string() -> None:
    assert _parse_media_refs("") == []


def test_parse_media_refs_returns_pairs_with_empty_mime() -> None:
    """Mime types are not persisted; recovery returns empty mime so the
    media pipeline re-derives from the stored file."""
    assert _parse_media_refs('["abc", "def"]') == [("abc", ""), ("def", "")]


def test_parse_media_refs_handles_invalid_json() -> None:
    assert _parse_media_refs("not-json") == []
    assert _parse_media_refs("{}") == []


# ---------------------------------------------------------------------------
# End-to-end recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_recover_dispatches_each_orphan(async_db: async_sessionmaker) -> None:
    """The recovery loop calls ``_dispatch_to_pipeline`` once per orphan
    and returns the count of successes.

    Setup runs through the async fixture because
    ``recover_orphan_inbound_messages`` now reads via an
    ``AsyncSession``: a sync write committed on the per-test sync
    transaction lives on a different connection and would not be
    visible under READ COMMITTED. See AGENTS.md "Cross-API caveat".
    """
    user = await _make_user_async(async_db)
    cs = await _make_session_async(async_db, user)
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2
    )
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1
    )
    expected_user_id = user.id

    captured_user_ids: list[str] = []
    captured_bodies: list[str] = []

    async def capture_dispatch(**kwargs: object) -> None:
        # Read the user id while the recovery's session is still open
        # in this same task (kwargs["user"] is detached but its id was
        # loaded before expunge, so this access is safe).
        captured_user_ids.append(kwargs["user"].id)  # type: ignore[attr-defined]
        captured_bodies.append(kwargs["message"].body)  # type: ignore[attr-defined]

    with patch(
        "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
        new=capture_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 2
    assert captured_user_ids == [expected_user_id, expected_user_id]
    assert set(captured_bodies) == {"one", "two"}


@pytest.mark.asyncio()
async def test_recover_short_circuits_when_lookback_zero() -> None:
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="should not run", minutes_ago=1)
    finally:
        db.close()

    with (
        patch.object(settings, "inbound_recovery_lookback_minutes", 0),
        patch(
            "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 0
    mock_dispatch.assert_not_awaited()


@pytest.mark.asyncio()
async def test_recover_continues_past_individual_dispatch_failure(
    async_db: async_sessionmaker,
) -> None:
    """One failing dispatch must not abort the whole sweep; the other
    orphan should still be recovered. Caller wraps the whole thing in a
    try/except too, but per-row resilience is what lets us deploy this
    in front of an in-flight queue without an all-or-nothing failure."""
    user = await _make_user_async(async_db)
    cs = await _make_session_async(async_db, user)
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2
    )
    await _add_message_async(
        async_db, cs, MessageDirection.INBOUND, seq=2, body="two", minutes_ago=1
    )

    call_count = {"n": 0}

    async def flaky_dispatch(**kwargs: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated dispatch failure")

    with patch(
        "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
        new=flaky_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    # Two attempts, one succeeded.
    assert call_count["n"] == 2
    assert recovered == 1


# ---------------------------------------------------------------------------
# Freshness floor (race with concurrent normal ingestion)
# ---------------------------------------------------------------------------


def test_freshness_floor_excludes_brand_new_inbounds() -> None:
    """A message that landed milliseconds ago is the normal ingestion
    path's responsibility, not recovery. Without this floor a worker
    could race the in-flight MessageBatcher and double-dispatch a message
    that was about to be processed normally."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="just now", minutes_ago=0)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        # Floor at -30s: anything newer is excluded as too-fresh.
        floor = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=30)
        rows = _run(_find_orphaned_inbounds_with_async_db(cutoff, floor))
    finally:
        db.close()

    assert rows == []


def test_freshness_floor_includes_messages_older_than_floor() -> None:
    """Sanity: floor at +0s (now) includes messages from the past."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="2m ago", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        floor = datetime.datetime.now(datetime.UTC)
        rows = _run(_find_orphaned_inbounds_with_async_db(cutoff, floor))
    finally:
        db.close()

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Idempotency (a successful dispatch should produce an outbound that
# masks the orphan on a subsequent sweep)
# ---------------------------------------------------------------------------


def test_orphan_is_no_longer_detected_after_outbound_lands() -> None:
    """The whole design rests on this invariant: once the agent loop runs
    and persists an outbound, the next sweep doesn't see the inbound as
    an orphan anymore. Simulate that by adding the outbound by hand and
    re-running the query."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="hi", minutes_ago=2)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        floor = datetime.datetime.now(datetime.UTC)
        before = _run(_find_orphaned_inbounds_with_async_db(cutoff, floor))
        assert len(before) == 1

        # Simulate the agent loop running and persisting its reply.
        _add_message(db, cs, MessageDirection.OUTBOUND, seq=2, body="ok", minutes_ago=1)

        after = _run(_find_orphaned_inbounds_with_async_db(cutoff, floor))
    finally:
        db.close()

    assert after == []


# ---------------------------------------------------------------------------
# Multi-worker advisory lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_recovery_skips_when_another_worker_holds_the_lock() -> None:
    """``pg_try_advisory_lock`` returns False to a second caller when
    another connection already holds the lock. The sweep must short-circuit
    in that case, otherwise N workers in a rolling restart would each
    re-dispatch the same orphans N times."""
    db = open_test_db_session()
    try:
        user = _make_user(db)
        cs = _make_session(db, user)
        _add_message(db, cs, MessageDirection.INBOUND, seq=1, body="one", minutes_ago=2)
    finally:
        db.close()

    with (
        patch(
            "backend.app.agent.inbound_recovery._try_acquire_lock",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "backend.app.agent.inbound_recovery._dispatch_to_pipeline",
            new=AsyncMock(),
        ) as mock_dispatch,
    ):
        recovered = await recover_orphan_inbound_messages()

    assert recovered == 0
    mock_dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Concurrency regression: pg_try_advisory_lock + pg_advisory_unlock pair
# ---------------------------------------------------------------------------


class TestInboundRecoveryLockSerialization:
    """Regression tests for the ``pg_try_advisory_lock`` /
    ``pg_advisory_unlock`` pair in ``inbound_recovery``.

    Mirrors ``TestApprovalLockSerialization`` in ``test_approval.py`` but
    targets the recovery-sweep lock instead of the approval-gate lock.
    The recovery lock differs in two important ways:

    * It is **session-scoped**, not transaction-scoped. ``pg_try_advisory_lock``
      keeps the lock until ``pg_advisory_unlock`` is called or the
      connection is closed; commits and rollbacks do not release it.
    * It is **non-blocking**. ``pg_try_advisory_lock`` returns ``True`` on
      acquisition and ``False`` if another session already holds the key,
      so contenders must short-circuit rather than wait. This is what
      lets a rolling restart skip duplicate sweeps cheaply: only the first
      worker to come up runs the recovery, the others see ``False`` and
      bail.

    Concurrency primitive: ``threading.Thread`` with ``threading.Event``
    and ``threading.Barrier`` coordination. The recovery code path is
    currently sync. When it converts to async (issue #1158), this matrix
    can be ported to ``asyncio.gather`` against ``AsyncSession`` with the
    same assertions.

    Database setup: each thread opens its own connection from the
    session-scoped ``_pg_engine``. ``pg_try_advisory_lock`` is a real
    Postgres feature scoped to the holding **session** (connection);
    sharing a single connection across threads would mean every
    ``pg_try_advisory_lock`` call returns ``True`` (recursive acquisition
    on the same session), so the threads need independent connections to
    actually exercise contention. The threads only call lock helpers (no
    INSERT / UPDATE), so nothing leaks past the test.

    No timestamp-based assertions here: ordering is established by
    ``threading.Event`` set / wait, never by comparing ``time.monotonic()``
    values across threads (see PR #1202 for the racy variant we are
    avoiding).
    """

    # Number of concurrent recovery contenders to spawn. Large enough that
    # if the lock primitive failed open, a duplicate would be statistically
    # very likely; small enough not to stress CI thread limits.
    _N_CONTENDERS = 5

    # Upper bound for any blocking wait. Tuned generously so a slow CI
    # runner does not flake.
    _TIMEOUT_S = 5.0

    def _try_lock_in_thread(
        self,
        engine: Engine,
        ready: threading.Event,
        release: threading.Event,
        result: dict[str, bool],
    ) -> None:
        """Acquire the recovery lock on a fresh connection, hold it until
        signaled, then release it on the **same** connection.

        Same-connection coupling matters: ``pg_advisory_unlock`` only
        releases a lock owned by the calling session. Releasing on a
        different connection is a silent no-op in Postgres.
        """
        connection = engine.connect()
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            connection.commit()
            result["acquired"] = acquired
            ready.set()
            if not acquired:
                return
            release.wait(timeout=self._TIMEOUT_S)
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            connection.commit()
        finally:
            connection.close()

    def _race_contender(
        self,
        engine: Engine,
        barrier: threading.Barrier,
        results: list[bool],
        results_lock: threading.Lock,
    ) -> None:
        """One of N contenders racing for the lock.

        Opens a fresh connection, waits at the barrier, then calls the
        production ``_try_acquire_lock`` helper. If acquired, releases via
        ``_release_lock`` on the same connection. Records the outcome.
        """

        class _ConnSession:
            """Minimal Session-shaped shim so ``_try_acquire_lock`` /
            ``_release_lock`` (which call ``.execute(...)`` and
            ``.commit()``) can run against a raw connection.

            The production helpers take a ``Session``, but in the test we
            need to control the underlying connection precisely (one per
            thread). The shim forwards the two methods the helpers use
            and nothing else.
            """

            def __init__(self, conn: object) -> None:
                self._conn = conn

            def execute(self, *args: object, **kwargs: object) -> object:
                return self._conn.execute(*args, **kwargs)  # type: ignore[attr-defined]

            def commit(self) -> None:
                self._conn.commit()  # type: ignore[attr-defined]

        connection = engine.connect()
        try:
            shim = _ConnSession(connection)
            barrier.wait(timeout=self._TIMEOUT_S)
            acquired = _try_acquire_lock_sync(shim)
            with results_lock:
                results.append(acquired)
            if acquired:
                _release_lock_sync(shim)
        finally:
            connection.close()

    def test_only_one_of_n_concurrent_attempts_acquires_lock(self, _pg_engine: Engine) -> None:
        """N threads racing for the recovery lock: exactly one acquires,
        the rest see it taken and exit. This is the core "no duplicate
        processing" invariant: under a rolling restart, if N workers boot
        simultaneously, only one runs the sweep.

        The first acquirer holds the lock until **all** contenders have
        finished their ``pg_try_advisory_lock`` call. That holding is what
        forces the others to observe ``False``. Without it, a lucky
        scheduler could let each thread acquire-release-acquire and all
        return ``True``, which would not exercise the contention path.
        """
        barrier = threading.Barrier(self._N_CONTENDERS + 1)
        results: list[bool] = []
        results_lock = threading.Lock()

        # Pre-acquire the lock on a holder connection so every contender
        # sees it taken. This is the deterministic shape: we do not rely
        # on whichever contender thread happens to win the race.
        holder_conn = _pg_engine.connect()
        try:
            held = bool(
                holder_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            holder_conn.commit()
            assert held, "holder thread failed to pre-acquire the lock"

            threads = [
                threading.Thread(
                    target=self._race_contender,
                    args=(_pg_engine, barrier, results, results_lock),
                )
                for _ in range(self._N_CONTENDERS)
            ]
            for t in threads:
                t.start()

            # Release the barrier so all contenders call
            # ``pg_try_advisory_lock`` as close to simultaneously as the
            # OS scheduler allows.
            barrier.wait(timeout=self._TIMEOUT_S)

            for t in threads:
                t.join(timeout=self._TIMEOUT_S)
                assert not t.is_alive(), "contender thread did not finish"
        finally:
            holder_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            holder_conn.commit()
            holder_conn.close()

        # All N contenders observed the lock as taken. Zero acquired.
        assert len(results) == self._N_CONTENDERS
        assert results.count(True) == 0, (
            f"expected zero contenders to acquire while holder held the lock, "
            f"got {results.count(True)} of {self._N_CONTENDERS}; "
            f"pg_try_advisory_lock did not exclude concurrent sessions"
        )

    def test_contender_succeeds_after_holder_releases(self, _pg_engine: Engine) -> None:
        """Sequencing check: while a holder thread owns the lock, a
        contender's ``pg_try_advisory_lock`` returns ``False``. After the
        holder releases, a fresh attempt on a new connection returns
        ``True``. This is the unblocking half of the no-duplicate
        invariant: the lock must actually free up between sweeps,
        otherwise a worker that crashed mid-sweep would poison every
        future restart on the same DB.
        """
        ready = threading.Event()
        release = threading.Event()
        holder_result: dict[str, bool] = {}

        holder = threading.Thread(
            target=self._try_lock_in_thread,
            args=(_pg_engine, ready, release, holder_result),
        )
        holder.start()
        try:
            assert ready.wait(timeout=self._TIMEOUT_S), "holder thread failed to acquire the lock"
            assert holder_result.get("acquired") is True

            # While the holder still owns the lock, a contender must not
            # acquire it.
            contender_conn = _pg_engine.connect()
            try:
                got = bool(
                    contender_conn.execute(
                        text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                        {"k": _RECOVERY_LOCK_KEY},
                    ).scalar()
                )
                contender_conn.commit()
                assert got is False, (
                    "contender acquired the lock while holder owned it; "
                    "pg_try_advisory_lock failed to exclude concurrent sessions"
                )
            finally:
                contender_conn.close()
        finally:
            release.set()
            holder.join(timeout=self._TIMEOUT_S)
            assert not holder.is_alive(), "holder thread did not release and exit"

        # Holder has released. A fresh attempt on a new connection now
        # acquires successfully.
        post_conn = _pg_engine.connect()
        try:
            got_after = bool(
                post_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            post_conn.commit()
            assert got_after is True, "lock was not released after holder thread exited"
            post_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            post_conn.commit()
        finally:
            post_conn.close()

    def test_unlock_on_different_connection_is_a_no_op(self, _pg_engine: Engine) -> None:
        """Lock-release coupling check: ``pg_advisory_unlock`` only
        releases a lock owned by the **same** session. Calling it on a
        different connection silently returns ``False`` and the lock
        stays held.

        This is the invariant the issue body calls out: under the async
        conversion, the lock acquisition and release must remain coupled
        to the same connection. If a future refactor accidentally routes
        ``_release_lock`` through a different ``open_test_db_session()`` than
        ``_try_acquire_lock``, the unlock becomes a silent no-op and the
        lock leaks for the lifetime of the original connection. This test
        encodes that coupling so a regression surfaces immediately.
        """
        holder_conn = _pg_engine.connect()
        wrong_conn = _pg_engine.connect()
        observer_conn = _pg_engine.connect()
        try:
            # Holder acquires.
            held = bool(
                holder_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            holder_conn.commit()
            assert held is True

            # Try to release on a different connection. Postgres returns
            # ``False`` here (lock not owned by this session). The release
            # is silently ineffective.
            unlocked = bool(
                wrong_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            wrong_conn.commit()
            assert unlocked is False, (
                "pg_advisory_unlock returned True on a non-owning connection; "
                "Postgres semantics changed and this test needs updating"
            )

            # The lock is still held: a third connection cannot acquire.
            still_held = bool(
                observer_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            observer_conn.commit()
            assert still_held is False, (
                "lock was released by an unlock on a different connection; "
                "the recovery code's same-connection coupling is broken"
            )

            # Releasing on the holder connection actually frees it.
            holder_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            holder_conn.commit()

            now_free = bool(
                observer_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _RECOVERY_LOCK_KEY},
                ).scalar()
            )
            observer_conn.commit()
            assert now_free is True, "lock did not free after release on the owning connection"
            observer_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            observer_conn.commit()
        finally:
            holder_conn.close()
            wrong_conn.close()
            observer_conn.close()
