"""Tests for the CalendarConfig model."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import backend.app.database as _db_module
from backend.app.models import CalendarConfig, User


@pytest.fixture()
def test_user() -> User:
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="cal-config-test-user", onboarding_complete=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    return user


def test_create_calendar_config(test_user: User) -> None:
    """Should create a CalendarConfig row."""
    db = _db_module.SessionLocal()
    try:
        config = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            display_name="My Calendar",
            calendar_id="primary",
            enabled=True,
        )
        db.add(config)
        db.commit()
        db.refresh(config)

        assert config.id is not None
        assert config.provider == "google_calendar"
        assert config.display_name == "My Calendar"
        assert config.calendar_id == "primary"
        assert config.enabled is True
        assert config.created_at is not None
    finally:
        db.close()


def test_unique_constraint_user_provider_calendar(test_user: User) -> None:
    """Should enforce unique (user_id, provider, calendar_id) constraint."""
    db = _db_module.SessionLocal()
    try:
        config1 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="primary",
        )
        db.add(config1)
        db.commit()

        # Same user, provider, AND calendar_id should fail
        config2 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="primary",
        )
        db.add(config2)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_multiple_calendars_per_user(test_user: User) -> None:
    """Same user+provider but different calendar_ids should be allowed."""
    db = _db_module.SessionLocal()
    try:
        config1 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="primary",
            display_name="Personal",
        )
        config2 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="jobs@example.com",
            display_name="Jobs",
        )
        db.add(config1)
        db.add(config2)
        db.commit()

        configs = (
            db.query(CalendarConfig)
            .filter_by(user_id=test_user.id, provider="google_calendar")
            .all()
        )
        assert len(configs) == 2
        cal_ids = {c.calendar_id for c in configs}
        assert cal_ids == {"primary", "jobs@example.com"}
    finally:
        db.close()


def test_cascade_delete_with_user(test_user: User) -> None:
    """CalendarConfig should be deleted when user is deleted."""
    db = _db_module.SessionLocal()
    try:
        config = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
        )
        db.add(config)
        db.commit()
        config_id = config.id

        # Delete the user
        user = db.get(User, test_user.id)
        assert user is not None
        db.delete(user)
        db.commit()

        # Config should be gone
        remaining = db.execute(
            select(CalendarConfig).where(CalendarConfig.id == config_id)
        ).scalar_one_or_none()
        assert remaining is None
    finally:
        db.close()
