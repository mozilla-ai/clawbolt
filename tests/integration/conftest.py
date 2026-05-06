"""Shared fixtures for integration tests that hit a real LLM API."""

import os

import pytest

from backend.app.models import User
from tests.db_test_utils import open_test_db_session

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

skip_without_anthropic_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


@pytest.fixture()
def integration_user() -> User:
    """Test user for integration tests (via DB)."""
    db = open_test_db_session()
    try:
        user = User(
            user_id="integration-test-user",
            phone="+15559999999",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


@pytest.fixture()
def onboarded_user() -> User:
    """Onboarded user for heartbeat tests (via DB)."""
    db = open_test_db_session()
    try:
        user = User(
            user_id="heartbeat-integration-user",
            phone="+15559990000",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()
