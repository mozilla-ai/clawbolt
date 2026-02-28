from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.router import handle_inbound_message
from backend.app.errors import (
    AgentError,
    BackshopError,
    MediaProcessingError,
    StorageError,
    TwilioError,
)
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.twilio_service import TwilioService
from tests.mocks.llm import make_text_response


@pytest.fixture()
def conversation(db_session: Session, test_contractor: Contractor) -> Conversation:
    conv = Conversation(contractor_id=test_contractor.id)
    db_session.add(conv)
    db_session.commit()
    db_session.refresh(conv)
    return conv


@pytest.fixture()
def inbound_message(db_session: Session, conversation: Conversation) -> Message:
    msg = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body="Hello, I need help",
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)
    return msg


@pytest.fixture()
def mock_twilio() -> TwilioService:
    service = TwilioService.__new__(TwilioService)
    service.client = MagicMock()
    service.from_number = "+15559876543"
    mock_msg = MagicMock()
    mock_msg.sid = "SM_test"
    service.client.messages.create.return_value = mock_msg
    return service


def test_exception_hierarchy() -> None:
    """Custom exceptions should inherit from BackshopError."""
    assert issubclass(MediaProcessingError, BackshopError)
    assert issubclass(AgentError, BackshopError)
    assert issubclass(StorageError, BackshopError)
    assert issubclass(TwilioError, BackshopError)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_llm_failure_returns_friendly_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """When agent LLM fails, should return a friendly error message."""
    mock_acompletion.side_effect = Exception("LLM API timeout")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        twilio_service=mock_twilio,
    )

    assert "trouble thinking" in response.reply_text
    assert "try again" in response.reply_text


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_twilio_media", new_callable=AsyncMock)
async def test_all_media_download_failure_adds_note(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """When all media downloads fail, context should include a note."""
    mock_download.side_effect = Exception("Download failed")
    mock_acompletion.return_value = make_text_response("Got your message!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("https://twilio.com/media/1.jpg", "image/jpeg")],
        twilio_service=mock_twilio,
    )

    # Agent should still process (text-only fallback)
    assert response.reply_text == "Got your message!"
    # The system note about download failure should have been in the context
    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    user_msg = call_args.kwargs["messages"][-1]["content"]
    assert "couldn't download" in user_msg


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
@patch("backend.app.agent.router.download_twilio_media", new_callable=AsyncMock)
async def test_partial_media_success(
    mock_download: AsyncMock,
    mock_vision: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """When some media succeeds and some fails, process what we can."""
    from backend.app.media.download import DownloadedMedia

    # First download succeeds, second fails
    mock_download.side_effect = [
        DownloadedMedia(
            content=b"good-image",
            mime_type="image/jpeg",
            original_url="https://twilio.com/1.jpg",
            filename="photo1.jpg",
        ),
        Exception("Download failed"),
    ]
    mock_vision.return_value = "A nice deck photo."
    mock_acompletion.return_value = make_text_response("I can see the deck!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[
            ("https://twilio.com/1.jpg", "image/jpeg"),
            ("https://twilio.com/2.jpg", "image/jpeg"),
        ],
        twilio_service=mock_twilio,
    )

    # Agent should still work with the one successful download
    assert response.reply_text == "I can see the deck!"
    mock_vision.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_twilio_send_failure_still_stores_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """When Twilio send fails, outbound message should still be stored."""
    mock_acompletion.return_value = make_text_response("Here's your answer!")  # type: ignore[union-attr]
    mock_twilio.client.messages.create.side_effect = Exception("Twilio outage")

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        twilio_service=mock_twilio,
    )

    # Response should still be returned
    assert response.reply_text == "Here's your answer!"

    # Outbound message should be stored even though SMS failed
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "Here's your answer!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.process_message_media", new_callable=AsyncMock)
@patch("backend.app.agent.router.download_twilio_media", new_callable=AsyncMock)
async def test_media_pipeline_failure_falls_back_to_text(
    mock_download: AsyncMock,
    mock_pipeline: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """When media pipeline crashes, should fall back to text-only processing."""
    from backend.app.media.download import DownloadedMedia
    from backend.app.media.pipeline import PipelineResult

    mock_download.return_value = DownloadedMedia(
        content=b"image",
        mime_type="image/jpeg",
        original_url="https://twilio.com/1.jpg",
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
    mock_acompletion.return_value = make_text_response("I can help!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("https://twilio.com/1.jpg", "image/jpeg")],
        twilio_service=mock_twilio,
    )

    assert response.reply_text == "I can help!"
    # Pipeline should have been called twice (first with media, then text-only fallback)
    assert mock_pipeline.call_count == 2
