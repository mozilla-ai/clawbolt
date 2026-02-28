"""Shared fixtures for e2e tests that hit real external services."""

import os

import pytest

from backend.app.config import Settings
from backend.app.services.telegram_service import TelegramMessagingService


def _telegram_credentials_available() -> bool:
    """Check if all required Telegram env vars are set."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


skip_without_telegram = pytest.mark.skipif(
    not _telegram_credentials_available(),
    reason="Telegram credentials not available (set TELEGRAM_BOT_TOKEN)",
)


@pytest.fixture()
def telegram_settings() -> Settings:
    """Build Settings from real env vars for e2e tests."""
    return Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
        messaging_provider="telegram",
    )


@pytest.fixture()
def telegram_service(telegram_settings: Settings) -> TelegramMessagingService:
    """Real TelegramMessagingService wired to actual Telegram Bot API."""
    return TelegramMessagingService(svc_settings=telegram_settings)


@pytest.fixture()
def test_chat_id() -> str:
    """The Telegram chat_id to test with."""
    return os.environ.get("TELEGRAM_TEST_CHAT_ID", "")
