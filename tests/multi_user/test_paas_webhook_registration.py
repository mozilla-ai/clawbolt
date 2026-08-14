"""Channel webhook auto-registration in the multi_user lifespan.

Verifies that the lifespan calls register_paas_webhook() on every
registered channel, so new channels get webhook registration
automatically without editing the lifespan.

Every test here patches ``backend.app.main.settings`` wholesale, which
means ``auth_mode`` has to be set explicitly: the lifespan reads it to
decide whether to register at all, and a bare MagicMock compares unequal
to every string.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from backend.app.main import lifespan


@pytest.fixture(autouse=True)
def _mock_lifespan_deps() -> None:  # type: ignore[misc]
    """Mock external dependencies so lifespan can run in tests."""
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
        patch("backend.app.main._verify_database"),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.discover_bot_username", new_callable=AsyncMock),
    ):
        yield


def _make_mock_channel(name: str, result: bool | None = None) -> MagicMock:
    """Create a mock channel with a register_paas_webhook method."""
    ch = MagicMock()
    ch.name = name
    ch.register_paas_webhook = AsyncMock(return_value=result)
    return ch


@pytest.mark.asyncio
async def test_register_paas_webhook_called_for_all_channels() -> None:
    """Premium lifespan should schedule register_paas_webhook on every channel.

    Registration runs as background tasks (so a slow / hung BlueBubbles
    server doesn't block lifespan startup), so the test yields briefly
    after entering the lifespan to let the AsyncMock-backed tasks run.
    """
    bb = _make_mock_channel("bluebubbles", True)
    linq = _make_mock_channel("linq", True)
    tg = _make_mock_channel("telegram", True)

    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    mock_manager.channels = {"telegram": tg, "linq": linq, "bluebubbles": bb}

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "https://app.example.com"
        mock_settings.telegram_bot_token = ""
        mock_settings.app_base_url = "https://app.clawbolt.ai"

        async with lifespan(FastAPI()):
            # Yield to the event loop so the background registration tasks
            # get a turn before lifespan shutdown cancels them.
            await asyncio.sleep(0.05)

    bb.register_paas_webhook.assert_called_once_with("https://app.clawbolt.ai")
    linq.register_paas_webhook.assert_called_once_with("https://app.clawbolt.ai")
    tg.register_paas_webhook.assert_called_once_with("https://app.clawbolt.ai")


@pytest.mark.asyncio
async def test_register_paas_webhook_skipped_on_localhost() -> None:
    """Premium lifespan should skip webhook registration on localhost."""
    bb = _make_mock_channel("bluebubbles", True)

    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    mock_manager.channels = {"bluebubbles": bb}

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "http://localhost:3000"
        mock_settings.telegram_bot_token = ""
        mock_settings.app_base_url = "http://localhost:8000"

        async with lifespan(FastAPI()):
            pass

    bb.register_paas_webhook.assert_not_called()


@pytest.mark.asyncio
async def test_register_paas_webhook_none_is_silent() -> None:
    """Channels returning None (not configured) should not log anything."""
    webchat = _make_mock_channel("webchat", None)

    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    mock_manager.channels = {"webchat": webchat}

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "https://app.example.com"
        mock_settings.telegram_bot_token = ""
        mock_settings.app_base_url = "https://app.clawbolt.ai"

        async with lifespan(FastAPI()):
            await asyncio.sleep(0.05)

    webchat.register_paas_webhook.assert_called_once_with("https://app.clawbolt.ai")


@pytest.mark.asyncio
async def test_register_paas_webhook_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Channels returning False should trigger a warning log."""
    bb = _make_mock_channel("bluebubbles", False)

    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    mock_manager.channels = {"bluebubbles": bb}

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "https://app.example.com"
        mock_settings.telegram_bot_token = ""
        mock_settings.app_base_url = "https://app.clawbolt.ai"

        import logging

        with caplog.at_level(logging.WARNING):
            async with lifespan(FastAPI()):
                await asyncio.sleep(0.05)

    assert any("bluebubbles webhook auto-registration failed" in msg for msg in caplog.messages)


@pytest.mark.asyncio
async def test_lifespan_calls_start_all_and_stop_all() -> None:
    """Lifespan should call manager.start_all() on startup and stop_all() on shutdown."""
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "https://example.com"
        mock_settings.telegram_bot_token = ""
        async with lifespan(FastAPI()):
            mock_manager.start_all.assert_awaited_once()

    mock_manager.stop_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_oauth_refresh_scheduler() -> None:
    """Premium lifespan must start and stop the OAuth refresh scheduler.

    Regression for issue #1087: premium replaces the OSS lifespan, so the
    scheduler wired into OSS main.py never runs in premium deployments
    unless lifespan calls start/stop directly.
    """
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()

    with (
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.get_manager", return_value=mock_manager),
        patch("backend.app.main.oauth_refresh_scheduler") as mock_oauth,
    ):
        mock_settings.auth_mode = "multi_user"
        mock_settings.cors_origins = "https://example.com"
        mock_settings.telegram_bot_token = ""
        async with lifespan(FastAPI()):
            mock_oauth.start.assert_called_once()
            mock_oauth.stop.assert_not_called()

    mock_oauth.stop.assert_called_once()
