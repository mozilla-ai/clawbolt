from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.approval import reset_approval_gate
from backend.app.agent.file_store import UserData, get_user_store, reset_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.bus import message_bus
from backend.app.config import settings
from backend.app.main import app
from backend.app.services.rate_limiter import webhook_rate_limiter


@pytest.fixture(autouse=True)
def _isolate_file_stores(tmp_path: object) -> Generator[None]:
    """Point file stores at a temp directory and reset caches for each test."""
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        reset_approval_gate()
        yield
    reset_stores()
    reset_approval_gate()


@pytest.fixture()
async def test_user(tmp_path: object) -> UserData:
    """Create a test user via the file store."""
    store = get_user_store()
    user = await store.create(
        user_id="test-user-001",
        phone="+15551234567",
        channel_identifier="123456789",
        preferred_channel="telegram",
        onboarding_complete=True,
    )
    return user


@pytest.fixture(autouse=True)
def _reset_bus_queues() -> Generator[None]:
    """Reset bus queues between tests so messages don't leak."""
    message_bus.reset()
    yield
    message_bus.reset()


@pytest.fixture()
def client(test_user: UserData) -> Generator[TestClient]:
    """FastAPI test client with overridden auth."""

    def _override_get_current_user() -> UserData:
        return test_user

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        # Default allowlist to "*" (allow all) so tests are not blocked.
        # Individual allowlist tests override these values.
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_ids", "*"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_usernames", ""),
        # Clear bot token so auto-derived webhook secret is empty for tests that
        # don't send a secret header
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        # Disable message batching in tests: the async batcher creates
        # fire-and-forget tasks that outlive the synchronous TestClient lifecycle.
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()
