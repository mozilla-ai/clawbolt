"""Test startup logging for webhook secret configuration."""

import logging
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app


def test_logs_auto_derived_secret_when_no_explicit_secret(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Startup should log INFO about auto-derived secret when no explicit secret is set."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    def _override_get_db() -> Generator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db

    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_effective_webhook_secret"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = ""
        mock_settings.cors_origins = "*"
        mock_settings.heartbeat_enabled = False

        with caplog.at_level(logging.INFO, logger="backend.app.main"), TestClient(app):
            pass

    assert any("auto-derived" in msg for msg in caplog.messages)
    assert not any("TELEGRAM_WEBHOOK_SECRET is not set" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_logs_configured_secret_when_explicit_secret_set(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Startup should log INFO about explicit secret when TELEGRAM_WEBHOOK_SECRET is set."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    def _override_get_db() -> Generator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db

    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_effective_webhook_secret"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = "my-secret"
        mock_settings.cors_origins = "*"
        mock_settings.heartbeat_enabled = False

        with caplog.at_level(logging.INFO, logger="backend.app.main"), TestClient(app):
            pass

    assert any("TELEGRAM_WEBHOOK_SECRET" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_warning_when_bot_token_not_set(caplog: "pytest.LogCaptureFixture") -> None:
    """No warning when bot token is empty (Telegram not configured at all)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    def _override_get_db() -> Generator[Session]:
        yield session

    app.dependency_overrides[get_db] = _override_get_db

    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_webhook_secret = ""
        mock_settings.cors_origins = "*"
        mock_settings.heartbeat_enabled = False

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("TELEGRAM_WEBHOOK_SECRET is not set" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()
