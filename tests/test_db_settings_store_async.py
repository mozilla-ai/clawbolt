"""Tests for the async API of ``DbSettingsStore`` (issue #1175).

Mirrors the DB-store half of ``tests/test_config_store.py`` for the
``*_async`` peers added in the dual-API rollout. All tests opt into the
per-test ``async_db`` fixture (see ``tests/conftest.py``) so writes are
rolled back at teardown. Follows the IdempotencyStore async pilot
(``tests/test_idempotency_pruning_async.py``, #1199) and the per-store
conversions in #1200, #1201, #1203.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.config_store import (
    ConfigStoreError,
    DbSettingsStore,
)
from backend.app.security.encryption import LocalKEKProvider, is_envelope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _kek_provider() -> LocalKEKProvider:
    """Deterministic provider for round-trip tests."""
    return LocalKEKProvider(key_material=b"x" * 32)


@pytest_asyncio.fixture()
async def _db_store(
    _kek_provider: LocalKEKProvider,
    async_db: async_sessionmaker,
) -> AsyncGenerator[DbSettingsStore]:
    """Construct a store wired to the per-test async session factory.

    The ``async_db`` fixture rebinds ``_async_session_factory`` for the
    async path. Passing the rebound factory explicitly (rather than
    relying on the deferred ``AsyncSessionLocal`` lookup) makes the
    test wiring obvious at the call site.
    """
    yield DbSettingsStore(_kek_provider, async_session_factory=async_db)


# ---------------------------------------------------------------------------
# Round-trip parity with the sync API
# ---------------------------------------------------------------------------


async def test_async_save_and_load_round_trips(_db_store: DbSettingsStore) -> None:
    """``save_async`` then ``load_async`` returns the exact value.

    Mirrors the load-bearing sync round-trip test: the integration
    guard against a future read/write split landing on the async path.
    """
    await _db_store.save_async(
        {
            "llm_provider": "anthropic",
            "llm_model": "claude-sonnet-4-6",
            "telegram_bot_token": "real-secret-token",
        }
    )
    loaded = await _db_store.load_async()
    assert loaded["llm_provider"] == "anthropic"
    assert loaded["llm_model"] == "claude-sonnet-4-6"
    # Decryption happened transparently on the async path too.
    assert loaded["telegram_bot_token"] == "real-secret-token"


async def test_async_secrets_are_envelope_encrypted_at_rest(
    _db_store: DbSettingsStore,
    async_db: async_sessionmaker,
) -> None:
    """The on-disk bytes are an envelope, not plaintext, when written async.

    Belt-and-suspenders: catches a regression where someone wires up
    ``save_async`` and forgets to route through the shared
    ``_build_save_rows`` helper that handles secret encryption.
    """
    await _db_store.save_async({"telegram_bot_token": "real-secret-token"})

    async with async_db() as db:
        row = (
            await db.execute(
                text("SELECT value, is_secret FROM app_settings WHERE key='telegram_bot_token'")
            )
        ).one()
    raw_value, is_secret_flag = row
    assert is_secret_flag is True
    assert "real-secret-token" not in raw_value
    assert is_envelope(raw_value)


async def test_async_non_secret_stored_verbatim(
    _db_store: DbSettingsStore,
    async_db: async_sessionmaker,
) -> None:
    """Non-secret values pass through unencrypted on the async path."""
    await _db_store.save_async({"llm_provider": "anthropic"})
    async with async_db() as db:
        row = (
            await db.execute(
                text("SELECT value, is_secret FROM app_settings WHERE key='llm_provider'")
            )
        ).one()
    assert row[0] == "anthropic"
    assert row[1] is False


async def test_async_save_rejects_non_persistable_keys(_db_store: DbSettingsStore) -> None:
    """``save_async`` raises ``ValueError`` before any IO for unknown keys.

    Same failure mode as ``save``: the sync path raises before opening
    a session; the async path must too. Uses the shared
    ``_build_save_rows`` helper, which validates up front.
    """
    with pytest.raises(ValueError, match="not a persistable setting"):
        await _db_store.save_async({"definitely_not_a_setting": "x"})


async def test_async_save_upserts(_db_store: DbSettingsStore) -> None:
    """Saving the same key twice updates the value, doesn't error on dup PK."""
    await _db_store.save_async({"llm_provider": "anthropic"})
    await _db_store.save_async({"llm_provider": "openai"})
    loaded = await _db_store.load_async()
    assert loaded["llm_provider"] == "openai"


async def test_async_delete_removes_rows(_db_store: DbSettingsStore) -> None:
    """``delete_async`` removes the named keys from the table."""
    await _db_store.save_async({"llm_provider": "anthropic", "llm_model": "sonnet"})
    await _db_store.delete_async(["llm_model"])
    loaded = await _db_store.load_async()
    assert "llm_provider" in loaded
    assert "llm_model" not in loaded


async def test_async_save_empty_is_noop(_db_store: DbSettingsStore) -> None:
    """An empty ``updates`` dict short-circuits before opening a session."""
    await _db_store.save_async({})
    assert await _db_store.load_async() == {}


async def test_async_delete_empty_is_noop(_db_store: DbSettingsStore) -> None:
    """``delete_async`` with no keys is a no-op (no SQL emitted)."""
    await _db_store.save_async({"llm_provider": "anthropic"})
    await _db_store.delete_async([])
    loaded = await _db_store.load_async()
    assert loaded["llm_provider"] == "anthropic"


async def test_async_save_records_actor(
    _db_store: DbSettingsStore,
    async_db: async_sessionmaker,
) -> None:
    """``actor_user_id`` is recorded against the row by the async path too."""
    from backend.app.models import User

    async with async_db() as db:
        user = User(
            user_id="async-actor-user-store",
            channel_identifier="async-actor-tel",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        actor_id = user.id

    await _db_store.save_async({"llm_provider": "anthropic"}, actor_user_id=actor_id)

    async with async_db() as db:
        row = (
            await db.execute(
                text("SELECT updated_by_user_id FROM app_settings WHERE key='llm_provider'")
            )
        ).one()
    assert row[0] == actor_id


async def test_async_load_returns_empty_for_empty_value(_db_store: DbSettingsStore) -> None:
    """Empty stored values short-circuit to "" without a decrypt call.

    Mirrors the sync ``load`` path so a row with ``value=""`` (the
    column's server default, e.g. after a schema migration that
    inserted blank rows) does not blow up trying to decrypt it as an
    envelope.
    """
    # Save a non-secret with empty value, then read it back.
    await _db_store.save_async({"llm_provider": ""})
    loaded = await _db_store.load_async()
    assert loaded["llm_provider"] == ""


# ---------------------------------------------------------------------------
# Per-test isolation canary (proves the async fixture rolls back)
# ---------------------------------------------------------------------------


async def test_async_isolation_rolls_back_between_tests_part_a(
    _db_store: DbSettingsStore,
) -> None:
    """Half 1 of a paired check that the async fixture rolls back.

    Writes a row; the paired test below must not see it.
    """
    await _db_store.save_async({"llm_provider": "iso-canary-anthropic"})
    loaded = await _db_store.load_async()
    assert loaded["llm_provider"] == "iso-canary-anthropic"


async def test_async_isolation_rolls_back_between_tests_part_b(
    _db_store: DbSettingsStore,
) -> None:
    """Half 2: confirms the previous test's row was rolled back."""
    loaded = await _db_store.load_async()
    assert "llm_provider" not in loaded


# ---------------------------------------------------------------------------
# Backend-failure path on the async API
# ---------------------------------------------------------------------------


async def test_async_load_raises_on_missing_table(
    _kek_provider: LocalKEKProvider,
) -> None:
    """``load_async`` wraps backend failure in ``ConfigStoreError``.

    Mirrors the sync ``test_db_store_load_raises_on_missing_table``:
    the contract is "raise loudly, don't silently return ``{}``", and
    the async path must hold the same line.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    # Point the store at a database URL that cannot resolve a
    # connection (TCP refused). The first ``execute`` will raise; the
    # store wraps it in ``ConfigStoreError``.
    bad_engine = create_async_engine(
        "postgresql+asyncpg://clawbolt:clawbolt@127.0.0.1:1/does_not_exist"
    )
    bad_factory: async_sessionmaker = async_sessionmaker(
        bind=bad_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        store = DbSettingsStore(
            kek_provider=_kek_provider,
            async_session_factory=bad_factory,
        )
        with pytest.raises(ConfigStoreError):
            await store.load_async()
    finally:
        await bad_engine.dispose()
