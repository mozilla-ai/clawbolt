"""Smoke tests for the database models and test infrastructure."""

from unittest.mock import patch

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import backend.app.database as _db_module
from backend.app.models import ChannelRoute, User
from tests.db_test_utils import get_test_async_db_url, open_test_db_session


def test_create_and_read_user() -> None:
    """Insert a User row and read it back."""
    db = open_test_db_session()
    try:
        user = User(user_id="alice@example.com", phone="+15551234567")
        db.add(user)
        db.flush()

        result = db.query(User).filter_by(user_id="alice@example.com").one()
        assert result.user_id == "alice@example.com"
        assert result.phone == "+15551234567"
        assert result.is_active is True
        assert result.onboarding_complete is False
        assert result.id is not None
    finally:
        db.close()


def test_channel_route_unique_constraint() -> None:
    """Duplicate (channel, channel_identifier) raises IntegrityError."""
    db = open_test_db_session()
    try:
        user = User(user_id="bob@example.com")
        db.add(user)
        db.flush()

        route1 = ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="12345")
        db.add(route1)
        db.flush()

        route2 = ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="12345")
        db.add(route2)
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.close()


def test_all_tables_created(_pg_engine: Engine) -> None:
    """All expected tables should exist after create_all."""
    expected = {
        "users",
        "channel_routes",
        "sessions",
        "messages",
        "media_files",
        "memory_documents",
        "heartbeat_logs",
        "idempotency_keys",
        "llm_usage_logs",
        "tool_configs",
    }
    with _pg_engine.connect() as conn:
        result = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        actual = {row[0] for row in result}
    assert expected <= actual


def test_sync_database_url_pins_psycopg3_driver() -> None:
    """``_sync_database_url`` routes bare and legacy URLs to psycopg3.

    SQLAlchemy 2.x still resolves ``postgresql://`` to psycopg2 even
    when only psycopg3 is installed, so we translate explicitly.
    """
    # Bare scheme is pinned to psycopg3.
    assert (
        _db_module._sync_database_url("postgresql://u:p@h:5432/db")
        == "postgresql+psycopg://u:p@h:5432/db"
    )
    # Legacy psycopg2 prefix is rewritten to psycopg3 for forward compat.
    assert (
        _db_module._sync_database_url("postgresql+psycopg2://u:p@h:5432/db")
        == "postgresql+psycopg://u:p@h:5432/db"
    )
    # Already-psycopg3 URLs pass through unchanged.
    assert (
        _db_module._sync_database_url("postgresql+psycopg://u:p@h:5432/db")
        == "postgresql+psycopg://u:p@h:5432/db"
    )
    # Async URLs are left alone (the async path handles them).
    assert (
        _db_module._sync_database_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )


def test_async_database_url_translates_postgresql_prefix() -> None:
    """``_async_database_url`` swaps the sync prefix for the asyncpg one."""
    assert (
        _db_module._async_database_url("postgresql://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )
    # psycopg3 (the new default sync driver) uses the ``+psycopg`` prefix.
    assert (
        _db_module._async_database_url("postgresql+psycopg://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )
    # Legacy psycopg2 prefix is still translated for forward compatibility.
    assert (
        _db_module._async_database_url("postgresql+psycopg2://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )
    # Already async-prefixed URLs pass through unchanged.
    assert (
        _db_module._async_database_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )


def test_async_engine_singleton_and_pool_settings() -> None:
    """``get_async_engine`` returns a singleton with async-specific pool args."""
    saved_engine = _db_module._async_engine
    _db_module._async_engine = None
    try:
        with patch.object(_db_module, "create_async_engine") as mock_create:
            _db_module.get_async_engine()
            _db_module.get_async_engine()  # second call must reuse the singleton

        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["pool_pre_ping"] is True
        assert kwargs["pool_recycle"] == 1800
        # asyncpg surfaces libpq's ``options`` parameter via
        # ``server_settings``; statement_timeout should still be set.
        assert kwargs["connect_args"] == {
            "server_settings": {"statement_timeout": "30000"},
        }
    finally:
        _db_module._async_engine = saved_engine


async def test_async_session_can_execute_trivial_select() -> None:
    """End-to-end smoke: an ``AsyncSession`` can run ``select(1)``.

    Builds its own engine so the test only exercises the async runtime
    wiring in ``backend.app.database``.
    """
    url = get_test_async_db_url()
    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    try:
        async with factory() as session:
            result = await session.execute(select(1))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_db_session_async_rollback_on_exception() -> None:
    """``db_session_async`` rolls back when the body raises.

    Mirrors the sync ``db_session`` lifecycle contract: exceptions
    trigger rollback before the session closes.
    """
    url = get_test_async_db_url()
    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    saved_factory = _db_module._async_session_factory
    _db_module._async_session_factory = factory
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with _db_module.db_session_async() as session:
                # Touch the connection so rollback is observable.
                await session.execute(select(1))
                raise RuntimeError("boom")
    finally:
        _db_module._async_session_factory = saved_factory
        await engine.dispose()


def test_user_defaults() -> None:
    """User model has correct defaults."""
    db = open_test_db_session()
    try:
        user = User(user_id="defaults@test.com")
        db.add(user)
        db.flush()

        assert user.preferred_channel == "telegram"
        assert user.heartbeat_opt_in is True
        assert user.heartbeat_frequency == "30m"
        assert user.soul_text == ""
        assert user.user_text == ""
        assert user.heartbeat_text == ""
        assert user.created_at is not None
        assert user.updated_at is not None
    finally:
        db.close()
