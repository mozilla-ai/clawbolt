"""Smoke tests for the database models and test infrastructure."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

import backend.app.database as _db_module
from backend.app.models import ChannelRoute, User


def test_create_and_read_user() -> None:
    """Insert a User row and read it back."""
    db = _db_module.SessionLocal()
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
    db = _db_module.SessionLocal()
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
        "clients",
        "estimates",
        "estimate_line_items",
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


def test_user_defaults() -> None:
    """User model has correct defaults."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="defaults@test.com")
        db.add(user)
        db.flush()

        assert user.preferred_channel == "telegram"
        assert user.heartbeat_opt_in is True
        assert user.heartbeat_frequency == "30m"
        assert user.folder_scheme == "by_client"
        assert user.soul_text == ""
        assert user.user_text == ""
        assert user.heartbeat_text == ""
        assert user.created_at is not None
        assert user.updated_at is not None
    finally:
        db.close()
