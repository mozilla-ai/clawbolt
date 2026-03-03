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


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_returns_empty_string_on_none_content(
    mock_acompletion: object,
) -> None:
    """analyze_image should return '' when LLM content is None, not None."""
    mock_acompletion.return_value = make_vision_response("")  # type: ignore[union-attr]
    # Manually set content to None to simulate LLM returning null content
    mock_acompletion.return_value.choices[0].message.content = None  # type: ignore[union-attr]
    result = await analyze_image(b"fake-jpeg-bytes", "image/jpeg")
    assert result == ""
    assert isinstance(result, str)


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_does_not_pass_api_key(mock_acompletion: object) -> None:
    """acompletion should be called without api_key so the SDK resolves keys from env."""
    mock_acompletion.return_value = make_vision_response("Test.")  # type: ignore[union-attr]
    await analyze_image(b"fake-jpeg-bytes", "image/jpeg")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert "api_key" not in call_args.kwargs


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_always_passes_user_when_provided(
    mock_acompletion: object,
) -> None:
    """When user is provided, it should always be passed to acompletion regardless of provider."""
    mock_acompletion.return_value = make_vision_response("Test.")  # type: ignore[union-attr]

    await analyze_image(b"fake-jpeg-bytes", "image/jpeg", user="42")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert call_args.kwargs["user"] == "42"


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
async def test_analyze_image_omits_user_when_not_provided(
    mock_acompletion: object,
) -> None:
    """When user is not provided, user should NOT be passed to acompletion."""
    mock_acompletion.return_value = make_vision_response("Test.")  # type: ignore[union-attr]

    await analyze_image(b"fake-jpeg-bytes", "image/jpeg")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert "user" not in call_args.kwargs


@pytest.mark.asyncio()
@patch("backend.app.media.vision.acompletion")
@patch("backend.app.media.vision.settings")
async def test_analyze_image_falls_back_to_llm_model(
    mock_settings: object, mock_acompletion: object
) -> None:
    """When vision_model is empty, should fall back to llm_model."""
    mock_settings.vision_model = ""  # type: ignore[attr-defined]
    mock_settings.llm_model = "claude-haiku-4-5-20251001"  # type: ignore[attr-defined]
    mock_settings.llm_provider = "anthropic"  # type: ignore[attr-defined]
    mock_settings.llm_api_base = None  # type: ignore[attr-defined]
    mock_acompletion.return_value = make_vision_response("Test.")  # type: ignore[union-attr]

    await analyze_image(b"fake-jpeg-bytes", "image/jpeg")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert call_args.kwargs["model"] == "claude-haiku-4-5-20251001"
