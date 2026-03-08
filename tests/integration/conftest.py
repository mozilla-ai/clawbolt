"""Shared fixtures for integration tests that hit a real LLM API."""

import asyncio
import os
from collections.abc import Generator
from unittest.mock import patch

import pytest

from backend.app.agent.file_store import ContractorData, get_contractor_store, reset_stores
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
def integration_contractor() -> ContractorData:
    """Test contractor for integration tests."""
    store = get_contractor_store()
    return asyncio.get_event_loop().run_until_complete(
        store.create(
            user_id="integration-test-user",
            name="Integration Test Contractor",
            phone="+15559999999",
            trade="General Contractor",
            location="Portland, OR",
        )
    )


@pytest.fixture()
def onboarded_contractor() -> ContractorData:
    """Onboarded contractor with business hours for heartbeat tests."""
    store = get_contractor_store()
    return asyncio.get_event_loop().run_until_complete(
        store.create(
            user_id="heartbeat-integration-user",
            name="Mike the Plumber",
            phone="+15559990000",
            trade="Plumber",
            location="Portland, OR",
            business_hours="7am-5pm",
            onboarding_complete=True,
        )
    )
