"""Tests for runtime webhook secret validation."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.app.agent.core import AgentResponse
from backend.app.agent.file_store import ContractorData, get_contractor_store, reset_stores
from backend.app.auth.dependencies import get_current_user
from backend.app.config import (
    Settings,
    _derive_webhook_secret,
    get_effective_webhook_secret,
    settings,
)
from backend.app.main import app
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.rate_limiter import check_webhook_rate_limit
from tests.mocks.telegram import make_telegram_update_payload

_MOCK_AGENT_RESPONSE = AgentResponse(reply_text="Mock reply")
_PATCH_HANDLE = "backend.app.agent.ingestion.handle_inbound_message"
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@contextmanager
def _make_client(
    *,
    webhook_secret: str = "",
    bot_token: str = "",
    data_dir: str = "/tmp/test_webhook_secret",
) -> Generator[TestClient]:
    """Build a TestClient with specific webhook secret / bot token settings."""
    with patch.object(settings, "contractor_data_dir", data_dir):
        reset_stores()

        store = get_contractor_store()
        contractor = asyncio.get_event_loop().run_until_complete(
            store.create(
                user_id="secret-test-user",
                name="Secret Test",
                phone="+15550000000",
                channel_identifier="999999",
                preferred_channel="telegram",
            )
        )

        def _override_get_current_user() -> ContractorData:
            return contractor

        mock_messaging = MagicMock(spec=MessagingService)
        mock_messaging.send_text = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_media = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_message = AsyncMock(return_value="mock_msg_id")
        mock_messaging.send_typing_indicator = AsyncMock()

        def _override_get_messaging_service() -> Generator[MessagingService]:
            yield mock_messaging

        app.dependency_overrides[get_current_user] = _override_get_current_user
        app.dependency_overrides[get_messaging_service] = _override_get_messaging_service
        app.dependency_overrides[check_webhook_rate_limit] = lambda: None

        with (
            patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
            patch(
                "backend.app.channels.telegram.settings.telegram_webhook_secret",
                webhook_secret,
            ),
            patch(
                "backend.app.channels.telegram.settings.telegram_bot_token",
                bot_token,
            ),
            patch(
                "backend.app.channels.telegram.settings.telegram_allowed_chat_ids",
                "*",
            ),
            patch(
                "backend.app.channels.telegram.settings.telegram_allowed_usernames",
                "",
            ),
            patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
            TestClient(app) as c,
        ):
            yield c

        app.dependency_overrides.clear()
        reset_stores()


# ---------------------------------------------------------------------------
# Integration tests: explicit secret
# ---------------------------------------------------------------------------


class TestExplicitSecretValidation:
    """Tests with an explicit TELEGRAM_WEBHOOK_SECRET."""

    def test_correct_secret_processes_message(self, tmp_path: object) -> None:
        """Request with the correct explicit secret should be processed."""
        with _make_client(webhook_secret="my-secret", data_dir=str(tmp_path)) as c:
            mock_handle = AsyncMock(return_value=_MOCK_AGENT_RESPONSE)
            with patch(_PATCH_HANDLE, mock_handle):
                payload = make_telegram_update_payload(chat_id=999999, message_id=1)
                resp = c.post(
                    "/api/webhooks/telegram",
                    json=payload,
                    headers={_SECRET_HEADER: "my-secret"},
                )
            assert resp.status_code == 200
            mock_handle.assert_called_once()

    def test_wrong_secret_rejects_silently(self) -> None:
        """Request with the wrong secret should return 200 but not process."""
        with _make_client(webhook_secret="my-secret") as c:
            mock_handle = AsyncMock(return_value=_MOCK_AGENT_RESPONSE)
            with patch(_PATCH_HANDLE, mock_handle):
                payload = make_telegram_update_payload(chat_id=999999, message_id=2)
                resp = c.post(
                    "/api/webhooks/telegram",
                    json=payload,
                    headers={_SECRET_HEADER: "wrong-secret"},
                )
            assert resp.status_code == 200
            mock_handle.assert_not_called()

    def test_missing_secret_header_rejects_silently(self) -> None:
        """Request without secret header should return 200 but not process."""
        with _make_client(webhook_secret="my-secret") as c:
            mock_handle = AsyncMock(return_value=_MOCK_AGENT_RESPONSE)
            with patch(_PATCH_HANDLE, mock_handle):
                payload = make_telegram_update_payload(chat_id=999999, message_id=3)
                resp = c.post("/api/webhooks/telegram", json=payload)
            assert resp.status_code == 200
            mock_handle.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tests: auto-derived secret
# ---------------------------------------------------------------------------


class TestAutoDerivedSecretValidation:
    """Tests where the secret is auto-derived from the bot token."""

    def test_auto_derived_secret_enforced(self, tmp_path: object) -> None:
        """When no explicit secret is set, the auto-derived secret should be enforced."""
        bot_token = "123456:ABC-DEF"
        derived = _derive_webhook_secret(bot_token)
        with _make_client(bot_token=bot_token, data_dir=str(tmp_path)) as c:
            mock_handle = AsyncMock(return_value=_MOCK_AGENT_RESPONSE)
            with patch(_PATCH_HANDLE, mock_handle):
                payload = make_telegram_update_payload(chat_id=999999, message_id=10)
                resp = c.post(
                    "/api/webhooks/telegram",
                    json=payload,
                    headers={_SECRET_HEADER: derived},
                )
            assert resp.status_code == 200
            mock_handle.assert_called_once()

    def test_auto_derived_secret_rejects_wrong_value(self) -> None:
        """Auto-derived secret should reject a wrong header value."""
        bot_token = "123456:ABC-DEF"
        with _make_client(bot_token=bot_token) as c:
            mock_handle = AsyncMock(return_value=_MOCK_AGENT_RESPONSE)
            with patch(_PATCH_HANDLE, mock_handle):
                payload = make_telegram_update_payload(chat_id=999999, message_id=11)
                resp = c.post(
                    "/api/webhooks/telegram",
                    json=payload,
                    headers={_SECRET_HEADER: "not-the-derived-secret"},
                )
            assert resp.status_code == 200
            mock_handle.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests: derivation and preference logic
# ---------------------------------------------------------------------------


class TestDeriveWebhookSecret:
    """Unit tests for _derive_webhook_secret."""

    def test_deterministic(self) -> None:
        """Same token should always produce the same secret."""
        token = "111:aaa"
        assert _derive_webhook_secret(token) == _derive_webhook_secret(token)

    def test_differs_by_token(self) -> None:
        """Different tokens should produce different secrets."""
        assert _derive_webhook_secret("111:aaa") != _derive_webhook_secret("222:bbb")


class TestGetEffectiveWebhookSecret:
    """Unit tests for get_effective_webhook_secret."""

    def test_prefers_explicit(self) -> None:
        """Explicit secret should take priority over derived."""
        s = Settings(
            telegram_bot_token="111:aaa",
            telegram_webhook_secret="explicit-secret",
        )
        assert get_effective_webhook_secret(s) == "explicit-secret"

    def test_derives_when_no_explicit(self) -> None:
        """Should derive from bot token when no explicit secret."""
        s = Settings(
            telegram_bot_token="111:aaa",
            telegram_webhook_secret="",
        )
        result = get_effective_webhook_secret(s)
        assert result == _derive_webhook_secret("111:aaa")
        assert result != ""

    def test_returns_empty_when_no_token(self) -> None:
        """Should return empty when neither secret nor token is set."""
        s = Settings(
            telegram_bot_token="",
            telegram_webhook_secret="",
        )
        assert get_effective_webhook_secret(s) == ""
