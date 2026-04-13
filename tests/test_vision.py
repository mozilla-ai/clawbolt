import base64
import io
import os
from unittest.mock import patch

import pytest
from any_llm.types.messages import MessageResponse, MessageUsage, TextBlock
from PIL import Image

from backend.app.media.vision import _MAX_RAW_BYTES, analyze_image, compress_image_for_api
from tests.mocks.llm import make_vision_response


def _make_noisy_jpeg(width: int = 100, height: int = 100, quality: int = 95) -> bytes:
    """Create a JPEG with random pixel data (resists compression)."""
    data = os.urandom(width * height * 3)
    img = Image.frombytes("RGB", (width, height), data)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_returns_description(mock_amessages: object) -> None:
    """analyze_image should return LLM description text."""
    mock_amessages.return_value = make_vision_response("A wooden deck with composite boards.")  # type: ignore[union-attr]
    result = await analyze_image(b"fake-jpeg-bytes", "image/jpeg")
    assert result == "A wooden deck with composite boards."
    mock_amessages.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_includes_context(mock_amessages: object) -> None:
    """analyze_image should include context in the request."""
    mock_amessages.return_value = make_vision_response("Deck damage visible.")  # type: ignore[union-attr]
    await analyze_image(b"fake-jpeg-bytes", "image/jpeg", context="What's wrong with this deck?")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    # System prompt is now the 'system' kwarg; messages[0] is the user message
    user_content = messages[0]["content"]
    text_parts = [p for p in user_content if p.get("type") == "text"]
    assert len(text_parts) == 1
    assert text_parts[0]["text"] == "What's wrong with this deck?"


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_encodes_base64(mock_amessages: object) -> None:
    """analyze_image should base64 encode the image bytes."""
    mock_amessages.return_value = make_vision_response("Test.")  # type: ignore[union-attr]
    await analyze_image(b"\x89PNG", "image/png")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    # System prompt is now the 'system' kwarg; messages[0] is the user message
    user_content = messages[0]["content"]
    image_parts = [p for p in user_content if p.get("type") == "image"]
    assert len(image_parts) == 1
    assert image_parts[0]["source"]["type"] == "base64"
    assert image_parts[0]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_returns_empty_string_on_none_content(
    mock_amessages: object,
) -> None:
    """analyze_image should return '' when LLM content has no text, not None."""
    # TextBlock validates text as str in 1.13+; use model_construct to bypass validation.
    block_none = TextBlock.model_construct(type="text", text=None)
    mock_amessages.return_value = MessageResponse.model_construct(  # type: ignore[union-attr]
        id="msg_mock",
        role="assistant",
        type="message",
        content=[block_none],
        model="mock-model",
        stop_reason="end_turn",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )
    result = await analyze_image(b"fake-jpeg-bytes", "image/jpeg")
    assert result == ""
    assert isinstance(result, str)


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_does_not_pass_api_key(mock_amessages: object) -> None:
    """amessages should be called without api_key so the SDK resolves keys from env."""
    mock_amessages.return_value = make_vision_response("Test.")  # type: ignore[union-attr]
    await analyze_image(b"fake-jpeg-bytes", "image/jpeg")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    assert "api_key" not in call_args.kwargs


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
@patch("backend.app.media.vision.settings")
async def test_analyze_image_falls_back_to_llm_model(
    mock_settings: object, mock_amessages: object
) -> None:
    """When vision_model is empty, should fall back to llm_model."""
    mock_settings.vision_model = ""  # type: ignore[attr-defined]
    mock_settings.llm_model = "claude-haiku-4-5-20251001"  # type: ignore[attr-defined]
    mock_settings.llm_provider = "anthropic"  # type: ignore[attr-defined]
    mock_settings.llm_api_base = None  # type: ignore[attr-defined]
    mock_settings.llm_max_tokens_vision = 1000  # type: ignore[attr-defined]
    mock_amessages.return_value = make_vision_response("Test.")  # type: ignore[union-attr]

    await analyze_image(b"fake-jpeg-bytes", "image/jpeg")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    assert call_args.kwargs["model"] == "claude-haiku-4-5-20251001"


# -- _build_vision_content unit tests --


def test_build_vision_content_without_context() -> None:
    """_build_vision_content without context should return only the image block."""
    from backend.app.media.vision import _build_vision_content

    blocks = _build_vision_content("AAAA", "image/jpeg")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["data"] == "AAAA"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"


def test_build_vision_content_with_context() -> None:
    """_build_vision_content with context should prepend a text block."""
    from backend.app.media.vision import _build_vision_content

    blocks = _build_vision_content("BBBB", "image/png", context="Describe this")
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "Describe this"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["data"] == "BBBB"


# -- compress_image_for_api tests --


def test_compress_small_image_unchanged() -> None:
    """Images already under the size limit should be returned unchanged."""
    small_jpeg = _make_noisy_jpeg(100, 100)
    assert len(small_jpeg) < _MAX_RAW_BYTES

    result_bytes, result_mime = compress_image_for_api(small_jpeg, "image/jpeg")
    assert result_bytes is small_jpeg
    assert result_mime == "image/jpeg"


def test_compress_large_image_fits_under_limit() -> None:
    """A large image should be compressed to fit under the API limit."""
    # Random noise at high quality produces a large JPEG.
    large_jpeg = _make_noisy_jpeg(4000, 3000, quality=98)
    assert len(large_jpeg) > _MAX_RAW_BYTES, (
        f"Test setup: need image > {_MAX_RAW_BYTES}, got {len(large_jpeg)}"
    )

    result_bytes, result_mime = compress_image_for_api(large_jpeg, "image/jpeg")
    assert len(result_bytes) <= _MAX_RAW_BYTES
    assert result_mime == "image/jpeg"
    # Verify it's a valid JPEG
    img = Image.open(io.BytesIO(result_bytes))
    assert img.format == "JPEG"


def test_compress_png_rgba_converts_to_jpeg() -> None:
    """An oversized RGBA PNG should be converted to JPEG (RGB)."""
    # Random noise in RGBA PNG is large and incompressible.
    data = os.urandom(2000 * 2000 * 4)
    img = Image.frombytes("RGBA", (2000, 2000), data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    large_png = buf.getvalue()
    assert len(large_png) > _MAX_RAW_BYTES, (
        f"Test setup: need image > {_MAX_RAW_BYTES}, got {len(large_png)}"
    )

    result_bytes, result_mime = compress_image_for_api(large_png, "image/png")
    assert len(result_bytes) <= _MAX_RAW_BYTES
    assert result_mime == "image/jpeg"
    result_img = Image.open(io.BytesIO(result_bytes))
    assert result_img.mode == "RGB"


@pytest.mark.asyncio()
@patch("backend.app.media.vision.amessages")
async def test_analyze_image_compresses_oversized_image(mock_amessages: object) -> None:
    """analyze_image should compress oversized images before sending to the API."""
    mock_amessages.return_value = make_vision_response("Noisy image.")  # type: ignore[union-attr]

    large_jpeg = _make_noisy_jpeg(4000, 3000, quality=98)
    assert len(large_jpeg) > _MAX_RAW_BYTES

    result = await analyze_image(large_jpeg, "image/jpeg")
    assert result == "Noisy image."

    # Verify the image sent to the LLM was compressed
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    user_content = messages[0]["content"]
    image_parts = [p for p in user_content if p.get("type") == "image"]
    assert len(image_parts) == 1
    sent_bytes = base64.b64decode(image_parts[0]["source"]["data"])
    assert len(sent_bytes) <= _MAX_RAW_BYTES
