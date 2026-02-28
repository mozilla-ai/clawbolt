from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.router import handle_inbound_message
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
        body="I need a quote for a 12x12 deck",
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


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_text_only_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """Text-only message should go through agent and produce reply."""
    mock_acompletion.return_value = make_text_response("I can help with that deck estimate!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        twilio_service=mock_twilio,
    )

    assert response.reply_text == "I can help with that deck estimate!"
    # Should have sent reply SMS
    mock_twilio.client.messages.create.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
@patch("backend.app.agent.router.download_twilio_media", new_callable=AsyncMock)
async def test_mms_with_photo(
    mock_download: AsyncMock,
    mock_vision: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """MMS with photo should download, process via vision, then agent."""
    from backend.app.media.download import DownloadedMedia

    mock_download.return_value = DownloadedMedia(
        content=b"fake-image",
        mime_type="image/jpeg",
        original_url="https://api.twilio.com/media/test.jpg",
        filename="photo.jpg",
    )
    mock_vision.return_value = "A 12x12 composite deck area."
    mock_acompletion.return_value = make_text_response("Looks like a great deck project!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("https://api.twilio.com/media/test.jpg", "image/jpeg")],
        twilio_service=mock_twilio,
    )

    assert response.reply_text == "Looks like a great deck project!"
    mock_download.assert_called_once()
    mock_vision.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_stores_outbound_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """Agent reply should be stored as outbound message."""
    mock_acompletion.return_value = make_text_response("Reply stored!")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        twilio_service=mock_twilio,
    )

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "Reply stored!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_twilio_media", new_callable=AsyncMock)
async def test_media_download_failure_still_processes_text(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_twilio: TwilioService,
) -> None:
    """If media download fails, agent should still process text."""
    mock_download.side_effect = Exception("Download failed")
    mock_acompletion.return_value = make_text_response("Got your text!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("https://api.twilio.com/media/fail.jpg", "image/jpeg")],
        twilio_service=mock_twilio,
    )

    assert response.reply_text == "Got your text!"
