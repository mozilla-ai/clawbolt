"""Shared fixtures for integration tests that hit a real LLM API."""

import os

import pytest
import pytest_asyncio

from backend.app.database import db_session_async
from backend.app.models import User

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

skip_without_anthropic_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


@pytest_asyncio.fixture()
async def integration_user() -> User:
    """Test user for integration tests (via DB)."""
    async with db_session_async() as db:
        user = User(
            user_id="integration-test-user",
            phone="+15559999999",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
        return user


@pytest_asyncio.fixture()
async def onboarded_user() -> User:
    """Onboarded user for heartbeat tests (via DB)."""
    async with db_session_async() as db:
        user = User(
            user_id="heartbeat-integration-user",
            phone="+15559990000",
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
        return user
