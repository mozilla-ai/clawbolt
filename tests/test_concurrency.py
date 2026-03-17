"""Tests for per-user processing lock."""

import asyncio
import time

import pytest

from backend.app.agent.concurrency import UserLockManager, user_locks


class TestUserLockManager:
    def test_acquire_creates_lock(self) -> None:
        """First acquire for a user should create a lock."""
        mgr = UserLockManager()
        lock = mgr.acquire("1")
        assert isinstance(lock, asyncio.Lock)
        assert mgr.active_count == 1

    def test_acquire_same_user_returns_same_lock(self) -> None:
        """Multiple acquires for the same user should return the same lock."""
        mgr = UserLockManager()
        lock1 = mgr.acquire("1")
        lock2 = mgr.acquire("1")
        assert lock1 is lock2

    def test_acquire_different_users_returns_different_locks(self) -> None:
        """Different users should get different locks."""
        mgr = UserLockManager()
        lock1 = mgr.acquire("1")
        lock2 = mgr.acquire("2")
        assert lock1 is not lock2
        assert mgr.active_count == 2

    @pytest.mark.asyncio
    async def test_same_user_serialized(self) -> None:
        """Two tasks for the same user should run sequentially."""
        mgr = UserLockManager()
        order: list[str] = []

        async def task_a() -> None:
            async with mgr.acquire("1"):
                order.append("a_start")
                await asyncio.sleep(0.05)
                order.append("a_end")

        async def task_b() -> None:
            # Small delay to ensure task_a acquires first
            await asyncio.sleep(0.01)
            async with mgr.acquire("1"):
                order.append("b_start")
                order.append("b_end")

        await asyncio.gather(task_a(), task_b())
        # task_a should fully complete before task_b starts
        assert order == ["a_start", "a_end", "b_start", "b_end"]

    @pytest.mark.asyncio
    async def test_different_users_parallel(self) -> None:
        """Two tasks for different users should run in parallel."""
        mgr = UserLockManager()
        order: list[str] = []

        async def task_a() -> None:
            async with mgr.acquire("1"):
                order.append("a_start")
                await asyncio.sleep(0.05)
                order.append("a_end")

        async def task_b() -> None:
            await asyncio.sleep(0.01)
            async with mgr.acquire("2"):
                order.append("b_start")
                await asyncio.sleep(0.01)
                order.append("b_end")

        await asyncio.gather(task_a(), task_b())
        # b should start before a ends (parallel)
        assert order.index("b_start") < order.index("a_end")

    def test_cleanup_removes_stale_locks(self) -> None:
        """Cleanup should remove locks that haven't been used recently."""
        mgr = UserLockManager(expiry_seconds=0)  # Expire immediately
        mgr.acquire("1")
        mgr.acquire("2")
        assert mgr.active_count == 2

        # Small delay so monotonic time advances
        time.sleep(0.01)
        removed = mgr.cleanup()
        assert removed == 2
        assert mgr.active_count == 0

    def test_cleanup_keeps_recent_locks(self) -> None:
        """Cleanup should keep locks that were recently used."""
        mgr = UserLockManager(expiry_seconds=3600)
        mgr.acquire("1")
        removed = mgr.cleanup()
        assert removed == 0
        assert mgr.active_count == 1

    @pytest.mark.asyncio
    async def test_cleanup_skips_locked(self) -> None:
        """Cleanup should not remove locks that are currently held."""
        mgr = UserLockManager(expiry_seconds=0)
        lock = mgr.acquire("1")
        await lock.acquire()
        try:
            time.sleep(0.01)
            removed = mgr.cleanup()
            assert removed == 0
            assert mgr.active_count == 1
        finally:
            lock.release()

    def test_module_singleton_exists(self) -> None:
        """The module-level singleton should be available."""
        assert isinstance(user_locks, UserLockManager)
