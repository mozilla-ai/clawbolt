import asyncio
import tempfile

from backend.app.config import settings


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe audio using faster-whisper.

    Args:
        audio_bytes: Raw audio data
        mime_type: Audio MIME type

    Returns:
        Transcribed text

    Raises:
        ImportError: If faster-whisper is not installed
    """
    return await asyncio.to_thread(_transcribe_sync, audio_bytes)


def _transcribe_sync(audio_bytes: bytes) -> str:
    """Synchronous transcription with faster-whisper."""
    try:
        from faster_whisper import WhisperModel  # type: ignore[unresolved-import]
    except ImportError:
        msg = (
            "faster-whisper is required for audio transcription. "
            "Install it with: pip install faster-whisper"
        )
        raise ImportError(msg) from None

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
