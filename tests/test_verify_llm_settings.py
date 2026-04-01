"""Tests for _verify_llm_settings startup check."""

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


@pytest.mark.asyncio
async def test_verify_llm_deduplicates_provider_model_pairs() -> None:
    """Identical (provider, model) pairs should only trigger one API call."""
    mock_amessages = AsyncMock(return_value=None)
    with (
        patch("backend.app.main.amessages", mock_amessages),
        patch("backend.app.main.settings") as mock_settings,
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-5.4-mini"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = None
        mock_settings.vision_provider = None
        # Same provider/model as primary: should be deduplicated.
        mock_settings.compaction_model = "gpt-5.4-mini"
        mock_settings.compaction_provider = "openai"
        mock_settings.heartbeat_model = None
        mock_settings.heartbeat_provider = None

        await _verify_llm_settings()

    assert mock_amessages.call_count == 1


@pytest.mark.asyncio
async def test_verify_llm_raises_for_primary_failure() -> None:
    """A failed primary model check should raise RuntimeError."""
    mock_amessages = AsyncMock(side_effect=Exception("bad key"))
    with (
        patch("backend.app.main.amessages", mock_amessages),
        patch("backend.app.main.settings") as mock_settings,
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-5.4-mini"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = None
        mock_settings.vision_provider = None
        mock_settings.compaction_model = None
        mock_settings.compaction_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.heartbeat_provider = None

        with pytest.raises(RuntimeError, match="LLM startup check failed for primary"):
            await _verify_llm_settings()


@pytest.mark.asyncio
async def test_verify_llm_warns_for_optional_model_failure() -> None:
    """A failed optional model check should warn, not raise."""
    call_count = 0

    async def _side_effect(**kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise Exception("vision model bad")

    mock_amessages = AsyncMock(side_effect=_side_effect)
    with (
        patch("backend.app.main.amessages", mock_amessages),
        patch("backend.app.main.settings") as mock_settings,
    ):
        mock_settings.llm_provider = "openai"
        mock_settings.llm_model = "gpt-5.4-mini"
        mock_settings.llm_api_base = None
        mock_settings.vision_model = "gpt-vision"
        mock_settings.vision_provider = "openai"
        mock_settings.compaction_model = None
        mock_settings.compaction_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.heartbeat_provider = None

        # Should not raise despite the vision model failure.
        await _verify_llm_settings()

    assert mock_amessages.call_count == 2
