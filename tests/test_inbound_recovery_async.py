"""Async tests for the inbound-recovery advisory-lock pair.

Mirrors ``tests/test_inbound_recovery.py::TestInboundRecoveryLockSerialization``
(PR #1206) for the async port added in issue #1158. The matrix is the
same: only one of N contenders acquires when a holder owns the lock,
and a contender succeeds after the holder releases.

The most load-bearing assertion is
``test_unlock_on_different_connection_is_a_no_op_async`` -- the async
port of the same-connection-coupling regression. The async sweep opens
a single ``AsyncConnection`` for the lock and routes both
``_try_acquire_lock_async`` and ``_release_lock_async`` through that
connection. If a future refactor splits acquire and release across
different ``AsyncConnection`` handles (or accidentally routes one
through an ``AsyncSession`` whose ``commit()`` returns the connection
to the pool), the unlock silently no-ops and the lock leaks for the
lifetime of the original connection. This test traps that the same
way the sync sibling does.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.agent.inbound_recovery import (
    _RECOVERY_LOCK_KEY,
    _release_lock_async,
    _try_acquire_lock_async,
)


class TestInboundRecoveryLockSerializationAsync:
    """Async port of ``TestInboundRecoveryLockSerialization``.

    Concurrency primitive: ``asyncio.Event`` and ``asyncio.gather``
    coordinated tasks. Each task opens its own ``AsyncConnection`` from
    the per-test ``_pg_async_engine`` so the contention runs through
    the real Postgres lock, not through a shared connection.

    These tests do NOT use the ``async_db`` fixture: ``async_db``
    rebinds the singleton factory to a single shared connection so test
    writes can be rolled back, which would defeat the parallelism check
    here. Each task instead opens its own connection from
    ``_pg_async_engine`` and closes it cleanly.
    """

    _N_CONTENDERS = 5
    _TIMEOUT_S = 5.0

    async def _try_lock_in_task(
        self,
        engine: AsyncEngine,
        ready: asyncio.Event,
        release: asyncio.Event,
        result: dict[str, bool],
    ) -> None:
        """Acquire the recovery lock on a fresh ``AsyncConnection``,
        hold it until signaled, then release it on the **same**
        connection. Mirrors the sync ``_try_lock_in_thread``."""
        connection = await engine.connect()
        try:
            acquired = await _try_acquire_lock_async(connection)
            result["acquired"] = acquired
            ready.set()
            if not acquired:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(release.wait(), timeout=self._TIMEOUT_S)
            await _release_lock_async(connection)
        finally:
            await connection.close()

    async def _race_contender(
        self,
        engine: AsyncEngine,
        barrier_event: asyncio.Event,
        results: list[bool],
        results_lock: asyncio.Lock,
    ) -> None:
        """One of N contenders racing for the lock. Opens a fresh
        ``AsyncConnection``, waits for the barrier event, then calls
        the production ``_try_acquire_lock_async``. If acquired,
        releases via ``_release_lock_async`` on the same connection."""
        connection = await engine.connect()
        try:
            await barrier_event.wait()
            acquired = await _try_acquire_lock_async(connection)
            async with results_lock:
                results.append(acquired)
            if acquired:
                await _release_lock_async(connection)
        finally:
            await connection.close()

    @pytest.mark.asyncio()
    async def test_only_one_of_n_concurrent_attempts_acquires_lock(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """N tasks racing for the recovery lock while a pre-acquired
        holder owns it: zero contenders acquire. This is the core
        no-duplicate-processing invariant."""
        barrier_event = asyncio.Event()
        results: list[bool] = []
        results_lock = asyncio.Lock()

        # Pre-acquire on a holder connection so every contender sees
        # the lock as taken.
        holder_conn = await _pg_async_engine.connect()
        try:
            held = await _try_acquire_lock_async(holder_conn)
            assert held, "holder task failed to pre-acquire the lock"

            tasks = [
                asyncio.create_task(
                    self._race_contender(_pg_async_engine, barrier_event, results, results_lock)
                )
                for _ in range(self._N_CONTENDERS)
            ]

            barrier_event.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=self._TIMEOUT_S)
        finally:
            await _release_lock_async(holder_conn)
            await holder_conn.close()

        assert len(results) == self._N_CONTENDERS
        assert results.count(True) == 0, (
            f"expected zero contenders to acquire while holder held the lock, "
            f"got {results.count(True)} of {self._N_CONTENDERS}; "
            f"pg_try_advisory_lock did not exclude concurrent sessions on "
            f"the async path"
        )

    @pytest.mark.asyncio()
    async def test_contender_succeeds_after_holder_releases(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """While a holder task owns the lock, a contender's
        ``pg_try_advisory_lock`` returns False. After the holder
        releases, a fresh attempt on a new connection returns True."""
        ready = asyncio.Event()
        release = asyncio.Event()
        holder_result: dict[str, bool] = {}

        holder_task = asyncio.create_task(
            self._try_lock_in_task(_pg_async_engine, ready, release, holder_result)
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=self._TIMEOUT_S)
            assert holder_result.get("acquired") is True

            contender_conn = await _pg_async_engine.connect()
            try:
                got = await _try_acquire_lock_async(contender_conn)
                assert got is False, (
                    "contender acquired the lock while holder owned it; "
                    "pg_try_advisory_lock failed to exclude concurrent "
                    "sessions on the async path"
                )
            finally:
                await contender_conn.close()
        finally:
            release.set()
            await asyncio.wait_for(holder_task, timeout=self._TIMEOUT_S)

        post_conn = await _pg_async_engine.connect()
        try:
            got_after = await _try_acquire_lock_async(post_conn)
            assert got_after is True, "lock was not released after holder task exited"
            await _release_lock_async(post_conn)
        finally:
            await post_conn.close()

    @pytest.mark.asyncio()
    async def test_unlock_on_different_connection_is_a_no_op_async(
        self,
        _pg_async_engine: AsyncEngine,
    ) -> None:
        """Lock-release coupling check on the async path:
        ``pg_advisory_unlock`` only releases a lock owned by the same
        Postgres session. Calling it on a different ``AsyncConnection``
        silently returns ``False`` and the lock stays held.

        This is the invariant the issue body calls out for the async
        conversion. If a future refactor accidentally routes
        ``_release_lock_async`` through a different ``AsyncConnection``
        than ``_try_acquire_lock_async`` (or routes either through an
        ``AsyncSession`` whose ``commit()`` returns the connection to
        the pool), the unlock becomes a silent no-op and the lock
        leaks for the lifetime of the original connection. This test
        encodes that coupling on the async side so a regression
        surfaces immediately.
        """
        holder_conn = await _pg_async_engine.connect()
        wrong_conn = await _pg_async_engine.connect()
        observer_conn = await _pg_async_engine.connect()
        try:
            # Holder acquires.
            held = await _try_acquire_lock_async(holder_conn)
            assert held is True

            # Try to release on a different connection. Postgres
            # returns ``False`` here (lock not owned by this session).
            # The release is silently ineffective.
            unlocked_result = await wrong_conn.execute(
                text("SELECT pg_advisory_unlock(hashtext(:k))"),
                {"k": _RECOVERY_LOCK_KEY},
            )
            unlocked = bool(unlocked_result.scalar())
            await wrong_conn.commit()
            assert unlocked is False, (
                "pg_advisory_unlock returned True on a non-owning connection; "
                "Postgres semantics changed and this test needs updating"
            )

            # The lock is still held: a third connection cannot acquire.
            still_held = await _try_acquire_lock_async(observer_conn)
            assert still_held is False, (
                "lock was released by an unlock on a different connection; "
                "the async recovery code's same-connection coupling is broken"
            )

            # Releasing on the holder connection actually frees it.
            await _release_lock_async(holder_conn)

            now_free = await _try_acquire_lock_async(observer_conn)
            assert now_free is True, "lock did not free after release on the owning connection"
            await _release_lock_async(observer_conn)
        finally:
            await holder_conn.close()
            await wrong_conn.close()
            await observer_conn.close()
