"""End-to-end tests against real Telegram Bot API.

Run with:
    uv run pytest -m e2e -v

Skip with:
    uv run pytest -m "not e2e"
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.file_store import reset_stores
from backend.app.channels.telegram import TelegramChannel
from backend.app.config import settings
from backend.app.main import app
from backend.app.services.messaging import MessagingService, get_messaging_service
from tests.mocks.llm import make_text_response
from tests.mocks.telegram import make_telegram_update_payload

from .conftest import skip_without_telegram

pytestmark = [pytest.mark.e2e, skip_without_telegram]


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_e2e_file_stores(tmp_path: object) -> Generator[None]:
    """Point file stores at a temp directory and reset caches for each e2e test."""
    with patch.object(settings, "data_dir", str(tmp_path)):
        reset_stores()
        yield
    reset_stores()


@pytest.fixture()
def e2e_client(
    telegram_service: TelegramChannel,
) -> Generator[TestClient]:
    """FastAPI TestClient wired to real Telegram but file-based storage."""

    def _override_get_messaging_service() -> Generator[MessagingService]:
        yield telegram_service

    app.dependency_overrides[get_messaging_service] = _override_get_messaging_service
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        # Disable webhook secret validation so e2e tests don't need to derive
        # and send the secret header (the e2e focus is Telegram round-trip).
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        # Allow all chat IDs through so the allowlist doesn't block e2e messages.
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_ids", "*"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_usernames", ""),
        # Disable message batching so background tasks complete synchronously.
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


# -- Service-level tests -------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_text_message(
    telegram_service: TelegramChannel,
    test_chat_id: str,
) -> None:
    """Send a real text message via Telegram and verify message_id returned."""
    if not test_chat_id:
        pytest.skip("TELEGRAM_TEST_CHAT_ID not set")
    msg_id = await telegram_service.send_text(
        to=test_chat_id,
        body="[clawbolt e2e] text delivery test",
    )
    assert msg_id.isdigit(), f"Expected numeric message_id, got: {msg_id}"


# -- Round-trip test -----------------------------------------------------------


def test_webhook_to_reply_round_trip(
    e2e_client: TestClient,
    test_chat_id: str,
) -> None:
    """Full round-trip: simulate inbound webhook -> agent replies -> real message sent.

    LLM is mocked for determinism. Telegram is real.
    """
    if not test_chat_id:
        pytest.skip("TELEGRAM_TEST_CHAT_ID not set")

    expected_reply = "[clawbolt e2e] I can help with that deck estimate!"

    with patch(
        "backend.app.agent.core.amessages",
        new_callable=AsyncMock,
        return_value=make_text_response(expected_reply),
    ):
        payload = make_telegram_update_payload(
            chat_id=int(test_chat_id),
            text="I need a quote for a 12x12 composite deck",
        )
        response = e2e_client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200
