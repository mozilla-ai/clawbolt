"""Tests for the CalendarConfig model."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.database import db_session_async
from backend.app.models import CalendarConfig, User


@pytest_asyncio.fixture()
async def test_user() -> User:
    async with db_session_async() as db:
        user = User(user_id="cal-config-test-user", onboarding_complete=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


async def test_create_calendar_config(test_user: User) -> None:
    """Should create a CalendarConfig row."""
    async with db_session_async() as db:
        config = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            display_name="My Calendar",
            calendar_id="primary",
            enabled=True,
        )
        db.add(config)
        await db.commit()
        await db.refresh(config)

        assert config.id is not None
        assert config.provider == "google_calendar"
        assert config.display_name == "My Calendar"
        assert config.calendar_id == "primary"
        assert config.enabled is True
        assert config.created_at is not None


async def test_unique_constraint_user_provider_calendar(test_user: User) -> None:
    """Should enforce unique (user_id, provider, calendar_id) constraint."""
    async with db_session_async() as db:
        config1 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="primary",
        )
        db.add(config1)
        await db.commit()

        # Same user, provider, AND calendar_id should fail
        config2 = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
            calendar_id="primary",
        )
        db.add(config2)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_multiple_calendars_per_user(test_user: User) -> None:
    """Same user+provider but different calendar_ids should be allowed."""
    async with db_session_async() as db:
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
        await db.commit()

        configs = (
            (
                await db.execute(
                    select(CalendarConfig).filter_by(
                        user_id=test_user.id, provider="google_calendar"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(configs) == 2
        cal_ids = {c.calendar_id for c in configs}
        assert cal_ids == {"primary", "jobs@example.com"}


async def test_cascade_delete_with_user(test_user: User) -> None:
    """CalendarConfig should be deleted when user is deleted."""
    async with db_session_async() as db:
        config = CalendarConfig(
            user_id=test_user.id,
            provider="google_calendar",
        )
        db.add(config)
        await db.commit()
        config_id = config.id

        # Delete the user
        user = await db.get(User, test_user.id)
        assert user is not None
        await db.delete(user)
        await db.commit()

        # Config should be gone
        remaining = (
            await db.execute(select(CalendarConfig).where(CalendarConfig.id == config_id))
        ).scalar_one_or_none()
        assert remaining is None
