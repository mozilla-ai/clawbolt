"""Regression test for #892: LLM startup check with low max_tokens."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.main import _verify_llm_settings


@pytest.mark.asyncio
async def test_verify_llm_uses_sufficient_max_tokens() -> None:
    """max_tokens must be high enough to avoid provider rejections.

    Regression test for #892: some provider/model combos reject max_tokens=1
    with a 400 error, causing the startup check to fail.
    """
    mock_amessages = AsyncMock(return_value=None)
    with (
        patch("backend.app.main.amessages", mock_amessages),
        patch("backend.app.main.settings") as mock_settings,
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-5.4-mini-2026-03-17"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = None
        mock_settings.vision_provider = None
        mock_settings.compaction_model = None
        mock_settings.compaction_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.heartbeat_provider = None

        await _verify_llm_settings()

    mock_amessages.assert_called_once()
    _, kwargs = mock_amessages.call_args
    assert kwargs["max_tokens"] >= 3, (
        f"max_tokens={kwargs['max_tokens']} is too low; some providers reject values below 3"
    )
