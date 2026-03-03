"""Integration tests for the vision/image pipeline against a real LLM.

Verifies that analyze_image() returns a meaningful description when given
a real image and that the full media pipeline handles real vision responses.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

import struct
import zlib
from unittest.mock import patch

import pytest

from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media
from backend.app.media.vision import analyze_image

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key

# Vision-capable model (Haiku 4.5 supports vision)
_VISION_MODEL = _ANTHROPIC_MODEL


def _make_png(width: int = 4, height: int = 4, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Generate a minimal valid PNG image programmatically.

    Creates a solid-color PNG without any external dependencies.
    """
    r, g, b = color

    # Build raw pixel data (each row: filter byte + RGB pixels)
    raw_data = b""
    for _ in range(height):
        raw_data += b"\x00"  # filter: None
        for _ in range(width):
            raw_data += bytes([r, g, b])

    def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        return (
            struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr_data)
        + _png_chunk(b"IDAT", zlib.compress(raw_data))
        + _png_chunk(b"IEND", b"")
    )


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_analyze_image_returns_description() -> None:
    """analyze_image() should return a non-empty string description from a real vision LLM."""
    png_bytes = _make_png(width=8, height=8, color=(0, 128, 255))

    with patch("backend.app.media.vision.settings") as mock_settings:
        mock_settings.vision_model = _VISION_MODEL
        mock_settings.llm_model = _VISION_MODEL
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_vision = 1000

        result = await analyze_image(png_bytes, "image/png")

    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_analyze_image_with_context() -> None:
    """analyze_image() should incorporate text context when provided."""
    png_bytes = _make_png(width=8, height=8, color=(139, 90, 43))

    with patch("backend.app.media.vision.settings") as mock_settings:
        mock_settings.vision_model = _VISION_MODEL
        mock_settings.llm_model = _VISION_MODEL
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_vision = 1000

        result = await analyze_image(
            png_bytes,
            "image/png",
            context="The contractor sent this photo of damage to a deck railing.",
        )

    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_pipeline_processes_real_image() -> None:
    """Full media pipeline should produce a combined context with real vision output."""
    png_bytes = _make_png(width=8, height=8, color=(200, 200, 200))

    media = DownloadedMedia(
        content=png_bytes,
        mime_type="image/png",
        original_url="test://integration-test-image",
        filename="test_image.png",
    )

    with patch("backend.app.media.vision.settings") as mock_settings:
        mock_settings.vision_model = _VISION_MODEL
        mock_settings.llm_model = _VISION_MODEL
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_vision = 1000

        result = await process_message_media(
            text_body="Here's a photo of the job site",
            media_items=[media],
        )

    assert len(result.media_results) == 1
    assert result.media_results[0].category == "image"
    # Vision should have produced real text, not the fallback
    assert result.media_results[0].extracted_text != "[Photo - vision analysis not available]"
    assert len(result.media_results[0].extracted_text) > 0
    assert "Photo 1" in result.combined_context


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_mime_mismatch_raises_error() -> None:
    """Claude rejects images where declared MIME type doesn't match actual content.

    This is a real production concern: if Telegram declares a file as image/jpeg
    but the content is actually PNG (or vice versa), the vision API will reject it.
    """
    png_bytes = _make_png(width=8, height=8, color=(0, 255, 0))

    with (
        patch("backend.app.media.vision.settings") as mock_settings,
        pytest.raises(Exception, match=r"image/jpeg.*image/png|invalid_request_error"),
    ):
        mock_settings.vision_model = _VISION_MODEL
        mock_settings.llm_model = _VISION_MODEL
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_vision = 1000

        # PNG bytes declared as JPEG — Claude validates and rejects this
        await analyze_image(png_bytes, "image/jpeg")
