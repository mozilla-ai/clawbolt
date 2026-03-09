from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import SessionState, StoredMessage, UserData
from backend.app.agent.router import handle_inbound_message
from backend.app.services.messaging import MessagingService
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
def mock_messaging() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.send_message = AsyncMock(return_value="msg_42")
    service.send_typing_indicator = AsyncMock()
    service.download_media = AsyncMock()
    return service


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_llm_failure_returns_friendly_message(
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """When agent LLM fails, should return a friendly error message."""
    mock_amessages.side_effect = Exception("LLM API timeout")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
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
    mock_messaging: MessagingService,
) -> None:
    """When all media downloads fail, context should include a note."""
    mock_messaging.download_media.side_effect = Exception("Download failed")  # type: ignore[union-attr]
    mock_amessages.return_value = make_text_response("Got your message!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        messaging_service=mock_messaging,
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
    mock_messaging: MessagingService,
) -> None:
    """When some media succeeds and some fails, process what we can."""
    from backend.app.media.download import DownloadedMedia

    # First download succeeds, second fails
    mock_messaging.download_media.side_effect = [  # type: ignore[union-attr]
        DownloadedMedia(
            content=b"good-image",
            mime_type="image/jpeg",
            original_url="AgACAgIAAxkBAAI_1",
            filename="photo1.jpg",
        ),
        Exception("Download failed"),
    ]
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
        messaging_service=mock_messaging,
    )

    # Agent should still work with the one successful download
    assert response.reply_text == "I can see the deck!"
    mock_vision.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_messaging_send_failure_still_stores_message(
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """When messaging send fails, outbound message should still be stored."""
    mock_amessages.return_value = make_text_response("Here's your answer!")  # type: ignore[union-attr]
    mock_messaging.send_text.side_effect = Exception("Messaging service outage")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    # Response should still be returned
    assert response.reply_text == "Here's your answer!"

    # Outbound message should be stored in the session
    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].body == "Here's your answer!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.process_message_media", new_callable=AsyncMock)
async def test_media_pipeline_failure_falls_back_to_text(
    mock_pipeline: AsyncMock,
    mock_amessages: object,
    test_user: UserData,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_messaging: MessagingService,
) -> None:
    """When media pipeline crashes, should fall back to text-only processing."""
    from backend.app.media.download import DownloadedMedia
    from backend.app.media.pipeline import PipelineResult

    mock_messaging.download_media.return_value = DownloadedMedia(  # type: ignore[union-attr]
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
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "I can help!"
    # Pipeline should have been called twice (first with media, then text-only fallback)
    assert mock_pipeline.call_count == 2
