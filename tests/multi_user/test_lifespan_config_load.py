"""Regression test: premium lifespan must hydrate settings before the LLM check.

Without ``store.load()`` / ``apply_to_settings()`` running before
``_verify_llm_settings()``, any setting that is not also exported as an
env var falls back to the Pydantic default (``""`` for ``llm_provider``
/ ``llm_model``) and startup fails. This was the original production
bug that motivated the SettingsStore refactor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from backend.app.main import lifespan


@pytest.mark.asyncio
async def test_lifespan_loads_settings_store_before_llm_check() -> None:
    """``store.load()`` must run before ``_verify_llm_settings()``."""
    call_order: list[str] = []

    async def record_load() -> dict[str, str]:
        call_order.append("store_load")
        return {}

    mock_store = MagicMock()
    mock_store.load = AsyncMock(side_effect=record_load)

    def record_apply(persisted: object) -> dict[str, str]:
        call_order.append("apply_to_settings")
        return {}

    async def record_verify() -> None:
        call_order.append("verify_llm")

    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()

    with (
        patch("backend.app.main.get_settings_store", return_value=mock_store),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", side_effect=record_apply),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", side_effect=record_verify),
        patch("backend.app.main._verify_database", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
        patch("backend.app.main.discover_bot_username", new_callable=AsyncMock),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.main.settings") as mock_premium,
    ):
        mock_settings.cors_origins = "https://app.example.com"
        mock_settings.telegram_bot_token = ""
        mock_premium.app_base_url = "http://localhost:8000"
        mock_premium.admin_user_ids = []

        async with lifespan(FastAPI()):
            pass

    # Both load and apply happen, and both happen before verify_llm.
    assert call_order.index("store_load") < call_order.index("verify_llm"), (
        "SettingsStore.load() must run before _verify_llm_settings() so "
        "settings.llm_provider / llm_model are hydrated before the LLM check."
    )
    assert call_order.index("apply_to_settings") < call_order.index("verify_llm")
