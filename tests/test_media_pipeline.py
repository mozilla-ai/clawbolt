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
@patch("backend.app.media.pipeline.transcribe_audio", new_callable=AsyncMock)
async def test_process_audio(mock_audio: AsyncMock) -> None:
    """Audio should be transcribed via faster-whisper."""
    mock_audio.return_value = "I need a quote for the deck."
    result = await process_message_media("", [_make_media("audio/ogg")])

    assert len(result.media_results) == 1
    assert result.media_results[0].category == "audio"
    assert result.media_results[0].extracted_text == "I need a quote for the deck."
    assert "Voice note" in result.combined_context


@pytest.mark.asyncio()
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
@patch("backend.app.media.pipeline.transcribe_audio", new_callable=AsyncMock)
async def test_process_mixed_media(mock_audio: AsyncMock, mock_vision: AsyncMock) -> None:
    """Multiple media types should all be processed."""
    mock_vision.return_value = "A backyard with a patio."
    mock_audio.return_value = "Standard railing, one set of stairs."

    media = [
        _make_media("image/jpeg", "https://example.com/photo.jpg"),
        _make_media("audio/ogg", "https://example.com/voice.ogg"),
    ]
    result = await process_message_media("12x12 deck", media)

    assert len(result.media_results) == 2
    assert result.combined_context.count("[") >= 3  # text + photo + voice note
    assert "Photo 1" in result.combined_context
    assert "Voice note" in result.combined_context


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
    "backend.app.media.pipeline.transcribe_audio",
    new_callable=AsyncMock,
    side_effect=ImportError("faster-whisper not installed"),
)
async def test_audio_graceful_when_whisper_missing(mock_audio: AsyncMock) -> None:
    """Audio processing should degrade gracefully when faster-whisper is not installed."""
    result = await process_message_media("Voice memo", [_make_media("audio/ogg")])
    assert len(result.media_results) == 1
    assert "not available" in result.media_results[0].extracted_text
    assert "faster-whisper" in result.media_results[0].extracted_text
