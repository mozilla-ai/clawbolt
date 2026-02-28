from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.router import handle_inbound_message
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response
from tests.mocks.storage import MockStorageBackend


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
def mock_messaging() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.send_message = AsyncMock(return_value="msg_42")
    return service


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_text_only_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Text-only message should go through agent and produce reply."""
    mock_acompletion.return_value = make_text_response("I can help with that deck estimate!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "I can help with that deck estimate!"
    mock_messaging.send_text.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_message_with_photo(
    mock_download: AsyncMock,
    mock_vision: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Message with photo should download, process via vision, then agent."""
    from backend.app.media.download import DownloadedMedia

    mock_download.return_value = DownloadedMedia(
        content=b"fake-image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_vision.return_value = "A 12x12 composite deck area."
    mock_acompletion.return_value = make_text_response("Looks like a great deck project!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        messaging_service=mock_messaging,
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
    mock_messaging: MessagingService,
) -> None:
    """Agent reply should be stored as outbound message."""
    mock_acompletion.return_value = make_text_response("Reply stored!")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "Reply stored!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_media_download_failure_still_processes_text(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """If media download fails, agent should still process text."""
    mock_download.side_effect = Exception("Download failed")
    mock_acompletion.return_value = make_text_response("Got your text!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "Got your text!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_processed_context_saved_to_message(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """processed_context should be saved to the Message after media pipeline."""
    mock_acompletion.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    db_session.refresh(inbound_message)
    assert inbound_message.processed_context is not None
    assert inbound_message.body in inbound_message.processed_context


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_file_tools_wired_when_storage_configured(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """File tools should be registered when storage credentials are set."""
    mock_settings.dropbox_access_token = "test-token"
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "gpt-4o"
    mock_settings.llm_provider = "openai"
    mock_get_storage.return_value = MockStorageBackend()
    mock_acompletion.return_value = make_text_response("File saved!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "File saved!"
    mock_get_storage.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.settings")
async def test_file_tools_skipped_when_no_storage(
    mock_settings: MagicMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """File tools should be skipped gracefully when storage not configured."""
    mock_settings.dropbox_access_token = ""
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "gpt-4o"
    mock_settings.llm_provider = "openai"
    mock_acompletion.return_value = make_text_response("No file tools!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == "No file tools!"
