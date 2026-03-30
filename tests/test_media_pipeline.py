from unittest.mock import AsyncMock, patch

import pytest

from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media


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
async def test_process_single_image(mock_vision: AsyncMock) -> None:
    """Single image should be processed via vision LLM."""
    mock_vision.return_value = "A composite deck with weathering."
    result = await process_message_media("Check this deck", [_make_media("image/jpeg")])

    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert result.media_results[0].extracted_text == "A composite deck with weathering."
    assert "Photo 1" in result.combined_context
    assert "Check this deck" in result.combined_context


@pytest.mark.asyncio()
async def test_process_text_only() -> None:
    """Text-only message (no media) should produce a simple context."""
    result = await process_message_media("Just a text message", [])
    assert result.text_body == "Just a text message"
    assert len(result.media_results) == 0
    assert "Just a text message" in result.combined_context


@pytest.mark.asyncio()
async def test_process_unknown_media_type() -> None:
    """Unknown media type should be skipped gracefully."""
    result = await process_message_media("", [_make_media("application/octet-stream")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "unknown"


@pytest.mark.asyncio()
@patch(
    "backend.app.media.pipeline.analyze_image",
    new_callable=AsyncMock,
    side_effect=RuntimeError("Vision API rate limit exceeded"),
)
async def test_image_vision_failure_produces_fallback(mock_vision: AsyncMock) -> None:
    """Vision API failures should produce a descriptive fallback instead of crashing."""
    result = await process_message_media("Check this roof", [_make_media("image/jpeg")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert "vision analysis not available" in result.media_results[0].extracted_text
    assert "Photo 1" in result.combined_context


@pytest.mark.asyncio()
@patch(
    "backend.app.media.pipeline.analyze_image",
    new_callable=AsyncMock,
    side_effect=TimeoutError("Connection timed out"),
)
async def test_image_timeout_produces_fallback(mock_vision: AsyncMock) -> None:
    """Vision API timeout should produce a fallback, not crash the pipeline."""
    result = await process_message_media("", [_make_media("image/png")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert "vision analysis not available" in result.media_results[0].extracted_text


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_image_document_classified_as_image(mock_vision: AsyncMock) -> None:
    """Images sent as documents with image/* MIME type should be classified as images."""
    mock_vision.return_value = "A photo of a damaged gutter."
    result = await process_message_media("", [_make_media("image/png")])
    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    assert result.media_results[0].extracted_text == "A photo of a damaged gutter."
    mock_vision.assert_called_once()
