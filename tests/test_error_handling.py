from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.file_store import SessionState, StoredMessage, UserData
from backend.app.agent.router import handle_inbound_message
from backend.app.bus import message_bus
from tests.mocks.llm import make_text_response


@pytest.fixture()
def conversation(test_user: UserData) -> SessionState:
    return SessionState(
        session_id="test-conv",
        user_id=test_user.id,
        is_active=True,
        messages=[
            StoredMessage(direction="inbound", body="Hello, I need help", seq=1),
        ],
    )


@pytest.fixture()
def inbound_message() -> StoredMessage:
    return StoredMessage(
        direction="inbound",
        body="Hello, I need help",
        seq=1,
    )


@pytest.fixture()
def mock_download_media() -> AsyncMock:
    return AsyncMock()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_llm_failure_returns_friendly_message(
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When agent LLM fails, should return a friendly error message."""
    mock_amessages.side_effect = Exception("LLM API timeout")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert "trouble thinking" in response.reply_text
    assert "try again" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_all_media_download_failure_adds_note(
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """When all media downloads fail, context should include a note."""
    mock_download_media.side_effect = Exception("Download failed")
    mock_amessages.return_value = make_text_response("Got your message!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        channel="telegram",
        download_media=mock_download_media,
    )

    # Agent should still process (text-only fallback)
    assert response.reply_text == "Got your message!"
    # The system note about download failure should have been in the context
    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    user_msg = call_args.kwargs["messages"][-1]["content"]
    assert "couldn't download" in user_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_partial_media_success(
    mock_vision: AsyncMock,
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When some media succeeds and some fails, process what we can."""
    from backend.app.media.download import DownloadedMedia

    # First download succeeds, second fails
    mock_download = AsyncMock(
        side_effect=[
            DownloadedMedia(
                content=b"good-image",
                mime_type="image/jpeg",
                original_url="AgACAgIAAxkBAAI_1",
                filename="photo1.jpg",
            ),
            Exception("Download failed"),
        ]
    )
    mock_vision.return_value = "A nice deck photo."
    mock_amessages.return_value = make_text_response("I can see the deck!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[
            ("AgACAgIAAxkBAAI_1", "image/jpeg"),
            ("AgACAgIAAxkBAAI_2", "image/jpeg"),
        ],
        channel="telegram",
        download_media=mock_download,
    )

    # Agent should still work with the one successful download
    assert response.reply_text == "I can see the deck!"
    mock_vision.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_outbound_stored_and_published_to_bus(
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Outbound reply should be both persisted in the session and published to the bus."""
    mock_amessages.return_value = make_text_response("Here's your answer!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # Response should still be returned
    assert response.reply_text == "Here's your answer!"

    # Outbound message should be stored in the session
    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].body == "Here's your answer!"

    # Outbound reply published to bus
    assert not message_bus.outbound.empty()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.process_message_media", new_callable=AsyncMock)
async def test_media_pipeline_failure_falls_back_to_text(
    mock_pipeline: AsyncMock,
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """When media pipeline crashes, should fall back to text-only processing."""
    from backend.app.media.download import DownloadedMedia
    from backend.app.media.pipeline import PipelineResult

    mock_download_media.return_value = DownloadedMedia(
        content=b"image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    # First call raises, second call (text-only fallback) succeeds
    mock_pipeline.side_effect = [
        Exception("Pipeline crash"),
        PipelineResult(
            text_body="Hello, I need help",
            media_results=[],
            combined_context="[Text message]: 'Hello, I need help'",
        ),
    ]
    mock_amessages.return_value = make_text_response("I can help!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        channel="telegram",
        download_media=mock_download_media,
    )

    assert response.reply_text == "I can help!"
    # Pipeline should have been called twice (first with media, then text-only fallback)
    assert mock_pipeline.call_count == 2
