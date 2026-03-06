"""Tests for LLM settings verification at startup."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app


def test_startup_succeeds_when_primary_model_is_valid(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Startup should succeed and log verification when the primary model works."""
    with (
        patch("backend.app.main.amessages", new_callable=AsyncMock) as mock_amessages,
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = ""
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.INFO, logger="backend.app.main"), TestClient(app):
            pass

        mock_amessages.assert_called_once()
        call_kwargs = mock_amessages.call_args
        assert call_kwargs.kwargs["provider"] == "openai"
        assert call_kwargs.kwargs["model"] == "gpt-4o"
        assert call_kwargs.kwargs["max_tokens"] == 1

    assert any("LLM verified (primary)" in msg for msg in caplog.messages)

    app.dependency_overrides.clear()


def test_startup_fails_when_primary_model_is_invalid() -> None:
    """Startup should raise RuntimeError when the primary model check fails."""
    with (
        patch(
            "backend.app.main.amessages",
            new_callable=AsyncMock,
            side_effect=Exception("model not found: bad-model"),
        ),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "bad-model"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = ""
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with (
            pytest.raises(RuntimeError, match="LLM startup check failed for primary model"),
            TestClient(app),
        ):
            pass

    app.dependency_overrides.clear()


def test_startup_warns_when_optional_model_is_invalid(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Optional model failures should warn but not block startup."""
    call_count = 0

    async def _selective_fail(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Primary model succeeds
            return
        # Vision model fails
        raise Exception("model not found: bad-vision")

    with (
        patch("backend.app.main.amessages", side_effect=_selective_fail),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = "bad-vision"
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.WARNING, logger="backend.app.main"), TestClient(app):
            pass

    assert any("LLM startup check failed for vision model" in msg for msg in caplog.messages)

    app.dependency_overrides.clear()


def test_deduplicates_identical_provider_model_pairs(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Identical (provider, model) pairs should only be checked once."""
    with (
        patch("backend.app.main.amessages", new_callable=AsyncMock) as mock_amessages,
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = ""
        # Compaction uses same provider/model as primary (explicit override to same values)
        mock_settings.compaction_model = "gpt-4o"
        mock_settings.compaction_provider = "openai"
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.INFO, logger="backend.app.main"), TestClient(app):
            pass

        # Only one call since (openai, gpt-4o) is deduplicated
        assert mock_amessages.call_count == 1

    app.dependency_overrides.clear()


def test_checks_all_distinct_model_configs(
    caplog: "pytest.LogCaptureFixture",
) -> None:
    """Each distinct (provider, model) pair should get its own verification call."""
    with (
        patch("backend.app.main.amessages", new_callable=AsyncMock) as mock_amessages,
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = "gpt-4o-vision"
        mock_settings.compaction_model = "gpt-4o-mini"
        mock_settings.compaction_provider = "openai"
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = "anthropic"
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with caplog.at_level(logging.INFO, logger="backend.app.main"), TestClient(app):
            pass

        # 4 distinct configs: primary, vision, compaction, heartbeat
        assert mock_amessages.call_count == 4

    app.dependency_overrides.clear()


def test_error_message_includes_env_var_names() -> None:
    """RuntimeError message should reference LLM_PROVIDER and LLM_MODEL env var names."""
    with (
        patch(
            "backend.app.main.amessages",
            new_callable=AsyncMock,
            side_effect=Exception("auth failed"),
        ),
        patch("backend.app.main.settings") as mock_settings,
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = ""
        mock_settings.compaction_model = ""
        mock_settings.compaction_provider = ""
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.telegram_bot_token = ""
        mock_settings.cors_origins = "*"

        with pytest.raises(RuntimeError, match=r"LLM_PROVIDER.*LLM_MODEL"), TestClient(app):
            pass

    app.dependency_overrides.clear()
