"""Test that a warning is logged when TELEGRAM_WEBHOOK_SECRET is not configured."""

import logging
from collections.abc import Generator
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app


def test_warns_when_webhook_secret_not_set(caplog: "logging.LogCaptureFixture") -> None:
    """Startup should log a warning when bot token is set but webhook secret is empty."""
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
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert any("TELEGRAM_WEBHOOK_SECRET is not set" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_warning_when_webhook_secret_set(caplog: "logging.LogCaptureFixture") -> None:
    """No warning should be logged when webhook secret is configured."""
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
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = "fake-bot-token"
        mock_settings.telegram_webhook_secret = "my-secret"
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("TELEGRAM_WEBHOOK_SECRET is not set" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_warning_when_bot_token_not_set(caplog: "logging.LogCaptureFixture") -> None:
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
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_webhook_secret = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("TELEGRAM_WEBHOOK_SECRET is not set" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()
