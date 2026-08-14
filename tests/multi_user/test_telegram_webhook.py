"""Tests for Telegram webhook registration, bot info, and admin webhook management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.models import Subscription
from backend.app.services import telegram_webhook as tw_module

# ---------------------------------------------------------------------------
# discover_bot_username
# ---------------------------------------------------------------------------


class TestDiscoverBotUsername:
    @pytest.fixture(autouse=True)
    def _reset_cache(self) -> None:
        tw_module._bot_username = ""

    @pytest.mark.asyncio
    async def test_discover_succeeds(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {"id": 123, "is_bot": True, "username": "TestBot"},
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.telegram_webhook.settings") as mock_settings,
            patch(
                "backend.app.services.telegram_webhook.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            mock_settings.telegram_bot_token = "fake:token"
            result = await tw_module.discover_bot_username()

        assert result == "TestBot"
        assert tw_module.get_bot_username() == "TestBot"

    @pytest.mark.asyncio
    async def test_discover_no_token(self) -> None:
        with patch("backend.app.services.telegram_webhook.settings") as mock_settings:
            mock_settings.telegram_bot_token = ""
            result = await tw_module.discover_bot_username()

        assert result is None
        assert tw_module.get_bot_username() == ""

    @pytest.mark.asyncio
    async def test_discover_api_failure(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "description": "Unauthorized"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.telegram_webhook.settings") as mock_settings,
            patch(
                "backend.app.services.telegram_webhook.httpx.AsyncClient",
                return_value=mock_client,
            ),
        ):
            mock_settings.telegram_bot_token = "bad:token"
            result = await tw_module.discover_bot_username()

        assert result is None


# ---------------------------------------------------------------------------
# register_webhook
# ---------------------------------------------------------------------------


class TestRegisterWebhook:
    @pytest.mark.asyncio
    async def test_register_with_explicit_url(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.telegram_webhook.settings") as mock_settings,
            patch(
                "backend.app.services.telegram_webhook.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "backend.app.services.telegram_webhook.get_effective_webhook_secret",
                return_value="secret123",
            ),
        ):
            mock_settings.telegram_bot_token = "fake:token"
            ok, url = await tw_module.register_webhook("https://example.com/api/webhooks/telegram")

        assert ok is True
        assert url == "https://example.com/api/webhooks/telegram"

    @pytest.mark.asyncio
    async def test_register_constructs_url_from_base(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.telegram_webhook.settings") as mock_settings,
            patch("backend.app.services.telegram_webhook.settings") as mock_premium,
            patch(
                "backend.app.services.telegram_webhook.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "backend.app.services.telegram_webhook.get_effective_webhook_secret",
                return_value="",
            ),
        ):
            mock_settings.telegram_bot_token = "fake:token"
            mock_premium.app_base_url = "https://app.clawbolt.ai"
            ok, url = await tw_module.register_webhook()

        assert ok is True
        assert url == "https://app.clawbolt.ai/api/webhooks/telegram"

    @pytest.mark.asyncio
    async def test_register_no_token(self) -> None:
        with patch("backend.app.services.telegram_webhook.settings") as mock_settings:
            mock_settings.telegram_bot_token = ""
            ok, url = await tw_module.register_webhook()

        assert ok is False
        assert url == ""

    @pytest.mark.asyncio
    async def test_register_api_failure(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "description": "Bad Request"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.services.telegram_webhook.settings") as mock_settings,
            patch(
                "backend.app.services.telegram_webhook.httpx.AsyncClient",
                return_value=mock_client,
            ),
            patch(
                "backend.app.services.telegram_webhook.get_effective_webhook_secret",
                return_value="",
            ),
        ):
            mock_settings.telegram_bot_token = "fake:token"
            ok, _url = await tw_module.register_webhook("https://example.com/hook")

        assert ok is False


# ---------------------------------------------------------------------------
# Admin webhook endpoints
# ---------------------------------------------------------------------------


class TestAdminTelegramWebhook:
    def test_register_webhook_success(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.register_webhook",
            new_callable=AsyncMock,
            return_value=(True, "https://app.clawbolt.ai/api/webhooks/telegram"),
        ):
            resp = client.post("/api/admin/telegram/webhook", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["webhook_url"] == "https://app.clawbolt.ai/api/webhooks/telegram"

    def test_register_webhook_with_custom_url(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.register_webhook",
            new_callable=AsyncMock,
            return_value=(True, "https://custom.example.com/hook"),
        ):
            resp = client.post(
                "/api/admin/telegram/webhook",
                json={"webhook_url": "https://custom.example.com/hook"},
            )
        assert resp.status_code == 200
        assert resp.json()["webhook_url"] == "https://custom.example.com/hook"

    def test_register_webhook_failure(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.register_webhook",
            new_callable=AsyncMock,
            return_value=(False, "https://app.clawbolt.ai/api/webhooks/telegram"),
        ):
            resp = client.post("/api/admin/telegram/webhook", json={})
        assert resp.status_code == 502

    def test_unregister_webhook_success(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.unregister_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = client.delete("/api/admin/telegram/webhook")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unregistered"
        assert data["webhook_url"] == ""

    def test_unregister_webhook_failure(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.unregister_webhook",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = client.delete("/api/admin/telegram/webhook")
        assert resp.status_code == 502

    def test_non_admin_cannot_register(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        test_subscription.role = "user"
        db_session.commit()
        resp = client.post("/api/admin/telegram/webhook", json={})
        assert resp.status_code == 403
        test_subscription.role = "admin"
        db_session.commit()
