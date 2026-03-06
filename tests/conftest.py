from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.file_store import ContractorData, get_contractor_store, reset_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.main import app
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.rate_limiter import webhook_rate_limiter


@pytest.fixture(autouse=True)
def _isolate_file_stores(tmp_path: object) -> Generator[None]:
    """Point file stores at a temp directory and reset caches for each test."""
    with patch.object(settings, "contractor_data_dir", str(tmp_path)):
        reset_stores()
        yield
    reset_stores()


@pytest.fixture()
async def test_contractor(tmp_path: object) -> ContractorData:
    """Create a test contractor via the file store."""
    store = get_contractor_store()
    return await store.create(
        user_id="test-user-001",
        name="Test Contractor",
        phone="+15551234567",
        trade="General Contractor",
        location="Portland, OR",
        channel_identifier="123456789",
        preferred_channel="telegram",
    )


@pytest.fixture()
def mock_messaging_service() -> MessagingService:
    """Mock MessagingService that doesn't hit real APIs."""
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="mock_msg_id")
    service.send_media = AsyncMock(return_value="mock_msg_id")
    service.send_message = AsyncMock(return_value="mock_msg_id")
    service.send_typing_indicator = AsyncMock()
    service.download_media = AsyncMock()
    return service


@pytest.fixture()
def client(
    test_contractor: ContractorData, mock_messaging_service: MessagingService
) -> Generator[TestClient]:
    """FastAPI test client with overridden auth and messaging."""

    def _override_get_current_user() -> ContractorData:
        return test_contractor

    def _override_get_messaging_service() -> Generator[MessagingService]:
        yield mock_messaging_service

    webhook_rate_limiter.reset()
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_messaging_service] = _override_get_messaging_service
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
