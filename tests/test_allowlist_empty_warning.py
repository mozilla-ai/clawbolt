"""Test that a warning is logged when the Telegram allowlist is empty."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


def test_warns_when_allowlist_empty(caplog: "pytest.LogCaptureFixture") -> None:
    """Startup should warn when bot token is set but allowlist is empty."""
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = "secret"
        mock_settings.telegram_allowed_chat_ids = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert any("All messages will be rejected" in msg for msg in caplog.messages)

    app.dependency_overrides.clear()


def test_no_warning_when_chat_ids_set(caplog: "pytest.LogCaptureFixture") -> None:
    """No allowlist warning when TELEGRAM_ALLOWED_CHAT_IDS is configured."""
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = "secret"
        mock_settings.telegram_allowed_chat_ids = "12345"
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("All messages will be rejected" in msg for msg in caplog.messages)

    app.dependency_overrides.clear()


def test_no_allowlist_warning_when_bot_token_not_set(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """No allowlist warning when bot token is empty (Telegram not configured)."""
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_webhook_secret = ""
        mock_settings.telegram_allowed_chat_ids = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("All messages will be rejected" in msg for msg in caplog.messages)

    app.dependency_overrides.clear()
