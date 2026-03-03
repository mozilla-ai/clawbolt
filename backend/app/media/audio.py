import asyncio
import tempfile

from faster_whisper import WhisperModel

from backend.app.config import settings


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe audio using faster-whisper.

    Args:
        audio_bytes: Raw audio data
        mime_type: Audio MIME type

    Returns:
        Transcribed text
    """
    return await asyncio.to_thread(_transcribe_sync, audio_bytes)


def _transcribe_sync(audio_bytes: bytes) -> str:
    """Synchronous transcription with faster-whisper."""
    model = WhisperModel(
        settings.whisper_model_size,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
    )

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        segments, _info = model.transcribe(tmp.name)
        return " ".join(segment.text.strip() for segment in segments)
