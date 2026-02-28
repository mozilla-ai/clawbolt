"""Test that a warning is logged when Telegram allowlists are empty."""

import logging
from collections.abc import Generator
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app


def test_warns_when_both_allowlists_empty(caplog: "logging.LogCaptureFixture") -> None:
    """Startup should warn when bot token is set but both allowlists are empty."""
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
        mock_settings.telegram_webhook_secret = "secret"
        mock_settings.telegram_allowed_chat_ids = ""
        mock_settings.telegram_allowed_usernames = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert any("No Telegram allowlist configured" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_warning_when_chat_ids_set(caplog: "logging.LogCaptureFixture") -> None:
    """No allowlist warning when TELEGRAM_ALLOWED_CHAT_IDS is configured."""
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
        mock_settings.telegram_webhook_secret = "secret"
        mock_settings.telegram_allowed_chat_ids = "12345"
        mock_settings.telegram_allowed_usernames = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("No Telegram allowlist configured" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_warning_when_usernames_set(caplog: "logging.LogCaptureFixture") -> None:
    """No allowlist warning when TELEGRAM_ALLOWED_USERNAMES is configured."""
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
        mock_settings.telegram_webhook_secret = "secret"
        mock_settings.telegram_allowed_chat_ids = ""
        mock_settings.telegram_allowed_usernames = "contractor1"
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("No Telegram allowlist configured" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()


def test_no_allowlist_warning_when_bot_token_not_set(
    caplog: "logging.LogCaptureFixture",
) -> None:
    """No allowlist warning when bot token is empty (Telegram not configured)."""
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
        mock_settings.telegram_allowed_chat_ids = ""
        mock_settings.telegram_allowed_usernames = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert not any("No Telegram allowlist configured" in msg for msg in caplog.messages)

    session.close()
    app.dependency_overrides.clear()
