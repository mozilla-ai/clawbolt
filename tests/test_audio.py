from unittest.mock import MagicMock, patch

import pytest

from backend.app.media.audio import transcribe_audio


@pytest.mark.asyncio()
@patch("backend.app.media.audio._transcribe_sync")
async def test_transcribe_audio(mock_transcribe: MagicMock) -> None:
    """transcribe_audio should return transcribed text."""
    mock_transcribe.return_value = "I need a quote for the deck repair at 123 Oak Street."
    result = await transcribe_audio(b"fake-audio-bytes")
    assert result == "I need a quote for the deck repair at 123 Oak Street."
    mock_transcribe.assert_called_once_with(b"fake-audio-bytes")


@pytest.mark.asyncio()
@patch("backend.app.media.audio._transcribe_sync")
async def test_transcribe_audio_empty(mock_transcribe: MagicMock) -> None:
    """transcribe_audio should handle empty transcription."""
    mock_transcribe.return_value = ""
    result = await transcribe_audio(b"silence")
    assert result == ""
