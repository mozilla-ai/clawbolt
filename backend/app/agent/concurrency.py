"""Per-contractor processing lock to prevent race conditions.

When two messages from the same contractor arrive in quick succession,
both background tasks could run the agent pipeline simultaneously,
causing duplicate memory saves, conflicting tool executions, and
interleaved responses.  This module provides a simple per-contractor
``asyncio.Lock`` manager that serializes processing for each contractor
while allowing different contractors to be processed in parallel.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Locks that haven't been used for this many seconds are eligible for cleanup
_LOCK_EXPIRY_SECONDS = 3600  # 1 hour


class _LockEntry:
    """An asyncio.Lock with a last-used timestamp for cleanup."""

    __slots__ = ("last_used", "lock")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()

    def touch(self) -> None:
        self.last_used = time.monotonic()


class ContractorLockManager:
    """Manages per-contractor asyncio locks.

    Usage::

        lock_manager = ContractorLockManager()

        async with lock_manager.acquire(contractor_id):
            await run_agent_pipeline(...)
    """

    def __init__(self, expiry_seconds: float = _LOCK_EXPIRY_SECONDS) -> None:
        self._locks: dict[int, _LockEntry] = {}
        self._expiry_seconds = expiry_seconds

    def acquire(self, contractor_id: int) -> asyncio.Lock:
        """Get the lock for a contractor, creating one if needed.

        Returns the ``asyncio.Lock`` itself so callers can use
        ``async with lock_manager.acquire(contractor_id):``.
        """
        entry = self._locks.get(contractor_id)
        if entry is None:
            entry = _LockEntry()
            self._locks[contractor_id] = entry
        entry.touch()
        return entry.lock

    def cleanup(self) -> int:
        """Remove locks that haven't been used recently.

        Returns the number of locks removed.
        """
        now = time.monotonic()
        stale = [
            cid
            for cid, entry in self._locks.items()
            if (now - entry.last_used) > self._expiry_seconds and not entry.lock.locked()
        ]
        for cid in stale:
            del self._locks[cid]
        if stale:
            logger.debug("Cleaned up %d stale contractor locks", len(stale))
        return len(stale)

    @property
    def active_count(self) -> int:
        """Number of currently tracked locks."""
        return len(self._locks)


# Module-level singleton
contractor_locks = ContractorLockManager()
