"""Shared fixtures for integration tests that hit a real LLM API."""

import asyncio
import os
from collections.abc import Generator
from unittest.mock import patch

import pytest

from backend.app.agent.file_store import UserData, get_user_store, reset_stores
from backend.app.config import settings

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

skip_without_anthropic_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


@pytest.fixture(autouse=True)
def _isolate_file_stores(tmp_path: object) -> Generator[None]:
    """Point file stores at a temp directory and reset caches for each test."""
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        yield
    reset_stores()


@pytest.fixture()
def integration_user() -> UserData:
    """Test user for integration tests."""
    store = get_user_store()
    return asyncio.get_event_loop().run_until_complete(
        store.create(
            user_id="integration-test-user",
            phone="+15559999999",
        )
    )


@pytest.fixture()
def onboarded_user() -> UserData:
    """Onboarded user for heartbeat tests."""
    store = get_user_store()
    return asyncio.get_event_loop().run_until_complete(
        store.create(
            user_id="heartbeat-integration-user",
            phone="+15559990000",
            onboarding_complete=True,
        )
    )
