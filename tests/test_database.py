"""Smoke tests for the database models and test infrastructure."""

from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.database import Base
from backend.app.models import ChannelRoute, User


@pytest.fixture()
def memory_engine() -> Generator[Engine]:
    """SQLite in-memory engine for unit tests that don't need real Postgres."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def memory_session(memory_engine: Engine) -> Generator[Session]:
    factory = sessionmaker(bind=memory_engine)
    session = factory()
    yield session
    session.close()


def test_create_and_read_user(memory_session: Session) -> None:
    """Insert a User row and read it back."""
    user = User(user_id="alice@example.com", phone="+15551234567")
    memory_session.add(user)
    memory_session.flush()

    result = memory_session.query(User).filter_by(user_id="alice@example.com").one()
    assert result.user_id == "alice@example.com"
    assert result.phone == "+15551234567"
    assert result.is_active is True
    assert result.onboarding_complete is False
    assert result.id is not None


def test_channel_route_unique_constraint(memory_session: Session) -> None:
    """Duplicate (channel, channel_identifier) raises IntegrityError."""
    from sqlalchemy.exc import IntegrityError

    user = User(user_id="bob@example.com")
    memory_session.add(user)
    memory_session.flush()

    route1 = ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="12345")
    memory_session.add(route1)
    memory_session.flush()

    route2 = ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="12345")
    memory_session.add(route2)
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_all_tables_created(memory_engine: Engine) -> None:
    """All 14 tables should exist after create_all."""
    expected = {
        "users",
        "channel_routes",
        "sessions",
        "messages",
        "clients",
        "estimates",
        "estimate_line_items",
        "media_files",
        "memory_documents",
        "heartbeat_items",
        "heartbeat_logs",
        "idempotency_keys",
        "llm_usage_logs",
        "tool_configs",
    }
    with memory_engine.connect() as conn:  # type: ignore[union-attr]
        # SQLite: check sqlite_master for table names
        result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        actual = {row[0] for row in result}
    assert expected <= actual


def test_user_defaults(memory_session: Session) -> None:
    """User model has correct defaults."""
    user = User(user_id="defaults@test.com")
    memory_session.add(user)
    memory_session.flush()

    assert user.preferred_channel == "telegram"
    assert user.heartbeat_opt_in is True
    assert user.heartbeat_frequency == "30m"
    assert user.folder_scheme == "by_client"
    assert user.soul_text == ""
    assert user.user_text == ""
    assert user.heartbeat_text == ""
    assert user.created_at is not None
    assert user.updated_at is not None
