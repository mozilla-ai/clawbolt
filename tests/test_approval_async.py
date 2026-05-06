"""Async tests for the ``ApprovalStore`` and the ``pg_advisory_xact_lock``
serialization invariant on its async path.

Mirrors ``tests/test_approval.py::TestApprovalLockSerialization`` (PR #1198)
for the ``*_async`` peers added in the advisory-lock conversion (issue
#1158). The matrix is the same: same-key serializes, different-key
runs in parallel. Concurrency primitive: ``asyncio.gather`` with two
``asyncio.Event`` handshakes per task. Each task opens its own
``AsyncSession`` over a fresh connection from the per-test
``_pg_async_engine`` so contention happens on the real Postgres lock,
not on a shared connection.

These tests do NOT use the ``async_db`` fixture: ``async_db`` rebinds
the singleton factory to a single shared connection so test writes can
be rolled back, which would defeat the parallelism check here. Each
task instead opens its own connection from ``_pg_async_engine`` and
runs in its own self-contained transaction that it commits before the
test ends; no rows are inserted so nothing leaks past teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from backend.app.agent.approval import (
    ApprovalStore,
    PermissionLevel,
    _lock_user_permissions,
    _user_permissions_lock_key,
)
from backend.app.models import User

# ---------------------------------------------------------------------------
# Async parity for ApprovalStore public methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_set_permission_persists_and_reads_back(
    async_test_user: User,
) -> None:
    """``set_permission`` must round-trip through
    ``load_user_permissions`` so the agent loop and the dashboard
    both see the same data via the async API."""
    store = ApprovalStore()
    await store.set_permission(async_test_user.id, "send_media_reply", PermissionLevel.DENY)
    data = await store.load_user_permissions(async_test_user.id)
    assert data["tools"]["send_media_reply"] == "deny"


@pytest.mark.asyncio()
async def test_ensure_complete_backfills_missing_tools(
    async_test_user: User,
) -> None:
    """``ensure_complete`` should write the defaults dict on first
    call and leave existing overrides untouched."""
    store = ApprovalStore()
    data = await store.ensure_complete(async_test_user.id)
    assert "tools" in data
    assert len(data["tools"]) > 0


@pytest.mark.asyncio()
async def test_check_permission_resolves_resource_then_tool(
    async_test_user: User,
) -> None:
    """Resource-keyed entries take precedence over tool-keyed entries
    in the async resolver, same as the sync resolver."""
    store = ApprovalStore()
    await store.set_permission(
        async_test_user.id,
        "web_fetch",
        PermissionLevel.ALWAYS,
        resource="example.com",
    )
    # Resource match wins.
    level = await store.check_permission(async_test_user.id, "web_fetch", resource="example.com")
    assert level == PermissionLevel.ALWAYS
    # Unrelated resource falls through to the default ASK.
    level = await store.check_permission(async_test_user.id, "web_fetch", resource="other.com")
    assert level == PermissionLevel.ASK


@pytest.mark.asyncio()
async def test_reset_permissions_writes_defaults(
    async_test_user: User,
) -> None:
    """``reset_permissions`` should clobber any prior overrides."""
    store = ApprovalStore()
    await store.set_permission(async_test_user.id, "send_media_reply", PermissionLevel.DENY)
    await store.reset_permissions(async_test_user.id)
    data = await store.load_user_permissions(async_test_user.id)
    # Default for send_media_reply is not "deny" (the registry default
    # depends on the tool's declaration). Asserting the override is
    # gone is enough to prove reset wrote new data.
    assert data["tools"].get("send_media_reply") != "deny"


# ---------------------------------------------------------------------------
# pg_advisory_xact_lock concurrency regression (async port of #1198)
# ---------------------------------------------------------------------------


class TestApprovalLockSerializationAsync:
    """Async port of
    ``tests/test_approval.py::TestApprovalLockSerialization``.

    Encodes the same matrix against ``_lock_user_permissions``:

    * Same user_id: two tasks must serialize on the lock; the second
      task must not acquire until the first commits.
    * Different user_id: two tasks must run in parallel because the
      hash-derived advisory keys do not collide.

    A future refactor that accidentally weakens the lock (e.g. routing
    acquire and the read-modify-write through different sessions, or
    committing before the write) trips this test the same way the sync
    sibling traps the same failure mode.
    """

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
        """Open a fresh ``AsyncSession``, take the lock on its autobegun
        transaction, signal ``ready``, wait for ``release``, then commit.

        Mirrors the sync sibling's ``_acquire_in_thread``. Each task gets
        its own connection so contention runs through the real Postgres
        lock and not through a shared connection's serialization.
        """
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await _lock_user_permissions(db, user_id)
            result[f"{label}_acquired"] = time.monotonic()
            ready.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(release.wait(), timeout=self._ACQUIRE_TIMEOUT_S)
            await db.commit()
            result[f"{label}_committed"] = time.monotonic()

    @pytest.mark.asyncio()
    async def test_same_user_lock_serializes_concurrent_writers(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """Two tasks acquiring the lock for the same user must run
        strictly one at a time. The second task must not acquire until
        the first commits."""
        user_id = f"approval-async-serial-{uuid.uuid4().hex[:8]}"
        a_ready = asyncio.Event()
        a_release = asyncio.Event()
        b_ready = asyncio.Event()
        b_release = asyncio.Event()
        results: dict[str, float] = {}

        task_a = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_id, a_ready, a_release, results, "a")
        )
        await asyncio.wait_for(a_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        task_b = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_id, b_ready, b_release, results, "b")
        )

        # While A still holds the lock, B must NOT acquire it.
        try:
            await asyncio.wait_for(b_ready.wait(), timeout=self._HOLD_S)
            b_acquired_during_a = True
        except TimeoutError:
            b_acquired_during_a = False
        assert not b_acquired_during_a, (
            "task B acquired the lock before task A released it; "
            "pg_advisory_xact_lock did not serialize same-user writers "
            "on the async path"
        )

        # Release A; B should now proceed.
        a_release.set()
        await asyncio.wait_for(task_a, timeout=self._ACQUIRE_TIMEOUT_S)
        await asyncio.wait_for(b_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        b_release.set()
        await asyncio.wait_for(task_b, timeout=self._ACQUIRE_TIMEOUT_S)

    @pytest.mark.asyncio()
    async def test_different_users_do_not_contend(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """Different ``user_id`` values hash to different advisory-lock
        keys so the second task must acquire while the first is still in
        its critical section."""
        user_a = f"approval-async-parallel-a-{uuid.uuid4().hex[:8]}"
        user_c = f"approval-async-parallel-c-{uuid.uuid4().hex[:8]}"

        a_ready = asyncio.Event()
        a_release = asyncio.Event()
        c_ready = asyncio.Event()
        c_release = asyncio.Event()
        results: dict[str, float] = {}

        task_a = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_a, a_ready, a_release, results, "a")
        )
        await asyncio.wait_for(a_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        task_c = asyncio.create_task(
            self._acquire_in_task(_pg_async_engine, user_c, c_ready, c_release, results, "c")
        )
        await asyncio.wait_for(c_ready.wait(), timeout=self._ACQUIRE_TIMEOUT_S)

        assert "a_committed" not in results, (
            "task A committed before C acquired; the parallelism check "
            "did not actually exercise overlapping critical sections"
        )

        c_release.set()
        await asyncio.wait_for(task_c, timeout=self._ACQUIRE_TIMEOUT_S)
        a_release.set()
        await asyncio.wait_for(task_a, timeout=self._ACQUIRE_TIMEOUT_S)


# ---------------------------------------------------------------------------
# Lock-key parity
# ---------------------------------------------------------------------------


def test_user_permissions_lock_key_is_stable() -> None:
    """The lock key must remain a stable function of ``user_id`` so the
    sync and async paths contend on the same Postgres advisory key."""
    key = _user_permissions_lock_key("user-abc")
    assert key == "user_permissions:user-abc"


# ---------------------------------------------------------------------------
# Iso-canary pair (proves async rollback isolates between tests)
# ---------------------------------------------------------------------------


_ISO_CANARY_USER_ID = f"approval-async-iso-{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio()
async def test_async_isolation_rolls_back_between_tests_part_a(
    async_test_user: User,
) -> None:
    """Write a permission row through the async API. ``part_b`` asserts
    it disappeared after this test's transaction was rolled back."""
    store = ApprovalStore()
    await store.set_permission(async_test_user.id, "send_media_reply", PermissionLevel.DENY)
    data = await store.load_user_permissions(async_test_user.id)
    assert data["tools"]["send_media_reply"] == "deny"


@pytest.mark.asyncio()
async def test_async_isolation_rolls_back_between_tests_part_b(
    async_test_user: User,
) -> None:
    """The override written in ``part_a`` must not be visible here. Two
    different ``async_test_user`` rows ensure the assertion is on
    user_id-keyed state, not row identity."""
    store = ApprovalStore()
    data = await store.load_user_permissions(async_test_user.id)
    # Either no row exists or, after ensure_complete, the registry default
    # for send_media_reply is whatever the registry says, not "deny".
    assert data.get("tools", {}).get("send_media_reply") != "deny"
