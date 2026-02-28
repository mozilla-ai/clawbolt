from unittest.mock import patch

import pytest

from backend.app.media.vision import analyze_image
from tests.mocks.llm import make_vision_response


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_returns_description(mock_acompletion: object) -> None:
    """analyze_image should return LLM description text."""
    mock_acompletion.return_value = make_vision_response("A wooden deck with composite boards.")  # type: ignore[union-attr]
    result = await analyze_image(b"fake-jpeg-bytes", "image/jpeg")
    assert result == "A wooden deck with composite boards."
    mock_acompletion.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_includes_context(mock_acompletion: object) -> None:
    """analyze_image should include context in the request."""
    mock_acompletion.return_value = make_vision_response("Deck damage visible.")  # type: ignore[union-attr]
    await analyze_image(b"fake-jpeg-bytes", "image/jpeg", context="What's wrong with this deck?")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    user_content = messages[1]["content"]
    text_parts = [p for p in user_content if p.get("type") == "text"]
    assert len(text_parts) == 1
    assert text_parts[0]["text"] == "What's wrong with this deck?"


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_encodes_base64(mock_acompletion: object) -> None:
    """analyze_image should base64 encode the image bytes."""
    mock_acompletion.return_value = make_vision_response("Test.")  # type: ignore[union-attr]
    await analyze_image(b"\x89PNG", "image/png")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    user_content = messages[1]["content"]
    image_parts = [p for p in user_content if p.get("type") == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
