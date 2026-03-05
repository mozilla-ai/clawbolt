from unittest.mock import patch

import pytest
from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage

from backend.app.media.vision import analyze_image
from tests.mocks.llm import make_vision_response


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
    # Build a response with no text blocks (empty content)
    mock_amessages.return_value = MessageResponse(  # type: ignore[union-attr]
        id="msg_mock",
        content=[MessageContentBlock(type="text", text=None)],
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
