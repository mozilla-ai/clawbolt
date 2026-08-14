"""End-to-end checks for the premium ``async_db`` fixture (issue #390).

Mirrors OSS ``tests/test_idempotency_pruning_async.py`` (the pilot from
mozilla-ai/clawbolt#1199). Validates that the async per-test fixture in
``tests/conftest.py``:

* Routes async store calls through a per-test ``AsyncConnection`` whose
  outer transaction is rolled back at teardown.
* Coexists with the sync ``_isolate_stores`` fixture (the sync override
  on ``_engine`` / ``_SessionLocal`` and the async override on
  ``_async_engine`` / ``_async_session_factory`` are disjoint).
* Provides isolation between tests (the canary pair below would fail if
  rollback were skipped).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.stores import IdempotencyStore
from backend.app.agent.user_db import UserStore
from backend.app.models import User

# ---------------------------------------------------------------------------
# Async store smoke (UserStore -- a method added in OSS #1201)
# ---------------------------------------------------------------------------


async def test_user_store_get_by_id_async_reads_async_test_user(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> None:
    """``UserStore.get_by_id_async`` reads a row inserted through ``async_db``.

    This is the smallest end-to-end check that the per-test
    ``AsyncConnection`` rebinding actually plumbs through to the OSS
    store's ``AsyncSessionLocal()`` calls.
    """
    store = UserStore()
    fetched = await store.get_by_id_async(async_test_user.id)
    assert fetched is not None
    assert fetched.id == async_test_user.id
    assert fetched.user_id == "async-test-user"


# ---------------------------------------------------------------------------
# Iso-canary pair: rollback isolation between tests
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    async_db: async_sessionmaker,
) -> None:
    """Half 1 of a paired check that the async fixture rolls back between tests.

    Writes a row; the paired test below must not see it.
    """
    store = IdempotencyStore()
    assert await store.try_mark_seen_async("premium-iso-canary") is True
    assert await store.has_seen_async("premium-iso-canary") is True


async def test_async_isolation_rolls_back_between_tests_part_b(
    async_db: async_sessionmaker,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    store = IdempotencyStore()
    assert await store.has_seen_async("premium-iso-canary") is False
