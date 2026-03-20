"""Tests for GET /api/channels/telegram/bot-info endpoint."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings


@pytest.fixture()
def _set_bot_token() -> Iterator[None]:
    """Ensure settings has a known bot token for tests that need it."""
    original = settings.telegram_bot_token
    settings.telegram_bot_token = "test-token-123"
    yield
    settings.telegram_bot_token = original


@pytest.fixture()
def _clear_bot_token() -> Iterator[None]:
    """Ensure settings has no bot token."""
    original = settings.telegram_bot_token
    settings.telegram_bot_token = ""
    yield
    settings.telegram_bot_token = original


def test_bot_info_returns_username(client: TestClient, _set_bot_token: None) -> None:
    """GET returns bot_username and bot_link when bot token is configured."""
    mock_me = AsyncMock()
    mock_me.username = "test_assistant_bot"

    with patch(
        "backend.app.channels.telegram.TelegramChannel.bot",
        new_callable=lambda: property(lambda self: _make_bot(mock_me)),
    ):
        resp = client.get("/api/channels/telegram/bot-info")

    assert resp.status_code == 200
    data = resp.json()
    assert data["bot_username"] == "test_assistant_bot"
    assert data["bot_link"] == "https://t.me/test_assistant_bot"


def test_bot_info_returns_404_without_token(client: TestClient, _clear_bot_token: None) -> None:
    """GET returns 404 when no bot token is configured."""
    resp = client.get("/api/channels/telegram/bot-info")
    assert resp.status_code == 404


def test_bot_info_returns_502_on_api_error(client: TestClient, _set_bot_token: None) -> None:
    """GET returns 502 when the Telegram API call fails."""
    mock_bot = AsyncMock()
    mock_bot.get_me = AsyncMock(side_effect=RuntimeError("Telegram API down"))

    with patch(
        "backend.app.channels.telegram.TelegramChannel.bot",
        new_callable=lambda: property(lambda self: mock_bot),
    ):
        resp = client.get("/api/channels/telegram/bot-info")

    assert resp.status_code == 502


class _FakeBot:
    """Minimal fake Bot that returns a canned getMe response."""

    def __init__(self, me: AsyncMock) -> None:
        self._me = me

    async def get_me(self) -> AsyncMock:
        return self._me


def _make_bot(me: AsyncMock) -> _FakeBot:
    return _FakeBot(me)
