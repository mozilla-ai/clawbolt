"""Tests for CORS wildcard startup warning."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from backend.app.main import lifespan


@pytest.fixture(autouse=True)
def _mock_lifespan_deps() -> None:  # type: ignore[misc]
    """Mock external dependencies so lifespan can run in tests.

    Patches get_manager and settings so the lifespan does not attempt
    real channel startup or webhook registration (which fails when APP_BASE_URL
    is set to a non-localhost value from .env).
    """
    _settings_store_mock = MagicMock()
    _settings_store_mock.load = AsyncMock(return_value={})
    _settings_store_mock.save = AsyncMock()
    _settings_store_mock.delete = AsyncMock()
    with (
        patch("backend.app.main.get_settings_store", return_value=_settings_store_mock),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", return_value={}),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager") as mock_manager,
        patch("backend.app.main.settings") as mock_premium,
    ):
        mock_manager.return_value.start_all = AsyncMock(return_value=[])
        mock_manager.return_value.stop_all = AsyncMock()
        # Prevent webhook registration branches from firing
        mock_premium.app_base_url = "http://localhost:8000"
        yield


@pytest.mark.asyncio
async def test_warns_when_cors_origins_is_wildcard(caplog: pytest.LogCaptureFixture) -> None:
    """Startup should warn when CORS_ORIGINS='*'."""
    with patch("backend.app.main.settings") as mock_settings:
        mock_settings.cors_origins = "*"
        mock_settings.telegram_bot_token = ""
        with caplog.at_level(logging.WARNING):
            async with lifespan(FastAPI()):
                pass
    assert any("CORS_ORIGINS" in msg for msg in caplog.messages)


@pytest.mark.asyncio
async def test_no_warning_when_cors_origins_is_specific(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup should not warn when CORS_ORIGINS is set to specific origins."""
    with patch("backend.app.main.settings") as mock_settings:
        mock_settings.cors_origins = "https://app.example.com"
        mock_settings.telegram_bot_token = ""
        with caplog.at_level(logging.WARNING):
            async with lifespan(FastAPI()):
                pass
    assert not any("CORS_ORIGINS" in msg for msg in caplog.messages)
