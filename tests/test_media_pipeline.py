from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent import media_staging
from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import (
    VISION_FALLBACK,
    process_message_media,
    run_vision_on_media,
)


def _make_media(
    mime_type: str = "image/jpeg", url: str = "https://example.com/media"
) -> DownloadedMedia:
    return DownloadedMedia(
        content=b"fake-bytes",
        mime_type=mime_type,
        original_url=url,
        filename="test_file",
    )


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_process_single_image_stages_without_vision(mock_vision: AsyncMock) -> None:
    """Pipeline classifies the image and leaves vision for the agent via
    analyze_photo. The combined context surfaces the staging handle."""
    media_staging.clear_user("test-user")
    media_staging.stage("test-user", "https://example.com/media", b"fake-bytes", "image/jpeg")
    result = await process_message_media(
        "Check this deck", [_make_media("image/jpeg")], user_id="test-user"
    )
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert result.media_results[0].extracted_text == ""
    assert mock_vision.await_count == 0
    assert "Photo 1" in result.combined_context
    assert "Check this deck" in result.combined_context
    assert "call analyze_photo" in result.combined_context
    media_staging.clear_user("test-user")


@pytest.mark.asyncio()
async def test_process_text_only() -> None:
    """Text-only message (no media) should produce a simple context."""
    result = await process_message_media("Just a text message", [])
    assert result.text_body == "Just a text message"
    assert len(result.media_results) == 0
    assert "Just a text message" in result.combined_context


@pytest.mark.asyncio()
async def test_process_unknown_media_type() -> None:
    """Unknown media type should be skipped gracefully with a placeholder."""
    result = await process_message_media("", [_make_media("application/octet-stream")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "unknown"


@pytest.mark.asyncio()
@patch(
    "backend.app.media.vision.analyze_image",
    new_callable=AsyncMock,
    side_effect=RuntimeError("Vision API rate limit exceeded"),
)
async def test_run_vision_on_media_failure_produces_fallback(mock_vision: AsyncMock) -> None:
    """run_vision_on_media (called by the analyze_photo tool) returns a fallback
    string when the vision API raises, instead of propagating the exception."""
    result = await run_vision_on_media(b"bytes", "image/jpeg", "Check this roof")
    assert result == VISION_FALLBACK


@pytest.mark.asyncio()
@patch(
    "backend.app.media.vision.analyze_image",
    new_callable=AsyncMock,
    side_effect=TimeoutError("Connection timed out"),
)
async def test_run_vision_on_media_timeout_produces_fallback(mock_vision: AsyncMock) -> None:
    """Timeouts from the vision API are caught and surface as the fallback."""
    result = await run_vision_on_media(b"bytes", "image/png", "")
    assert result == VISION_FALLBACK


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_image_document_classified_as_image(mock_vision: AsyncMock) -> None:
    """Images sent as documents with image/* MIME type should be classified as
    images. Vision is never called from the pipeline."""
    result = await process_message_media("", [_make_media("image/png")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert mock_vision.await_count == 0
