import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm import AuthenticationError, ContentFilterError
from sqlalchemy.orm import Session

from backend.app.agent.router import (
    AUTH_ERROR_FALLBACK,
    CONTENT_FILTER_FALLBACK,
    handle_inbound_message,
)
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response, make_tool_call_response
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
    # Verify user (contractor ID) is passed for OpenAI tracking
    call_kwargs = mock_vision.call_args
    assert call_kwargs.kwargs.get("user") == str(test_contractor.id)


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
    mock_settings.storage_provider = "dropbox"
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
    mock_settings.storage_provider = "dropbox"
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


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch(
    "backend.app.media.pipeline.analyze_image",
    new_callable=AsyncMock,
    side_effect=RuntimeError("Vision API down"),
)
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_pipeline_failure_note_mentions_vision(
    mock_download: AsyncMock,
    mock_vision: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When media pipeline fails, the system note should mention vision analysis."""
    from backend.app.media.download import DownloadedMedia

    mock_download.return_value = DownloadedMedia(
        content=b"fake-image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )

    # Make process_message_media raise to trigger the fallback path
    with patch(
        "backend.app.agent.router.process_message_media",
        new_callable=AsyncMock,
    ) as mock_pipeline:
        # First call raises, second call (fallback) succeeds
        from backend.app.media.pipeline import PipelineResult

        mock_pipeline.side_effect = [
            RuntimeError("Pipeline crashed"),
            PipelineResult(
                text_body="Check this",
                media_results=[],
                combined_context="[Text message]: 'Check this'",
            ),
        ]
        mock_acompletion.return_value = make_text_response("I see you sent something!")  # type: ignore[union-attr]

        await handle_inbound_message(
            db=db_session,
            contractor=test_contractor,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            messaging_service=mock_messaging,
        )

    # The system note should be specific about vision analysis
    db_session.refresh(inbound_message)
    assert "Vision analysis was unavailable" in inbound_message.processed_context


# ---------------------------------------------------------------------------
# Error handling path tests (issue #138)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_media_download_failure_adds_system_note_to_context(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When all media downloads fail, the persisted context includes the download-failure note."""
    mock_download.side_effect = Exception("Network timeout")
    mock_acompletion.return_value = make_text_response("Got your text!")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[("file_id_1", "image/jpeg")],
        messaging_service=mock_messaging,
    )

    db_session.refresh(inbound_message)
    assert "couldn't download" in inbound_message.processed_context.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_multiple_media_partial_download_failure_no_download_note(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When some media downloads succeed and others fail, no download-failure note is added."""
    from backend.app.media.download import DownloadedMedia

    mock_download.side_effect = [
        DownloadedMedia(
            content=b"image-bytes",
            mime_type="image/jpeg",
            original_url="file_ok",
            filename="photo.jpg",
        ),
        Exception("Download failed for second file"),
    ]
    mock_acompletion.return_value = make_text_response("Got one photo!")  # type: ignore[union-attr]

    with patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = "A photo of a deck."
        response = await handle_inbound_message(
            db=db_session,
            contractor=test_contractor,
            message=inbound_message,
            media_urls=[("file_ok", "image/jpeg"), ("file_bad", "image/png")],
            messaging_service=mock_messaging,
        )

    assert response.reply_text == "Got one photo!"
    # downloaded_media is not empty, so the "couldn't download" note is NOT added
    db_session.refresh(inbound_message)
    assert "couldn't download" not in inbound_message.processed_context.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_media_pipeline_failure_retries_with_empty_media(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When process_message_media raises, it retries with an empty media list."""
    from backend.app.media.download import DownloadedMedia
    from backend.app.media.pipeline import PipelineResult

    mock_download.return_value = DownloadedMedia(
        content=b"image-bytes",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_acompletion.return_value = make_text_response("Text only fallback!")  # type: ignore[union-attr]

    with patch(
        "backend.app.agent.router.process_message_media",
        new_callable=AsyncMock,
    ) as mock_pipeline:
        fallback_result = PipelineResult(
            text_body=inbound_message.body,
            media_results=[],
            combined_context=f"[Text message]: '{inbound_message.body}'",
        )
        mock_pipeline.side_effect = [
            RuntimeError("Pipeline exploded"),
            fallback_result,
        ]

        response = await handle_inbound_message(
            db=db_session,
            contractor=test_contractor,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            messaging_service=mock_messaging,
        )

    assert response.reply_text == "Text only fallback!"
    # The fallback call should have been made with empty media list
    assert mock_pipeline.call_count == 2
    second_call_args = mock_pipeline.call_args_list[1]
    assert second_call_args[0][1] == []  # second positional arg is empty media list


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_storage_exception_skips_file_tools(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When get_storage_service() raises, file tools are skipped and processing continues."""
    mock_settings.storage_provider = "dropbox"
    mock_settings.dropbox_access_token = "some-token"
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "gpt-4o"
    mock_settings.llm_provider = "openai"
    mock_get_storage.side_effect = RuntimeError("Storage backend init failed")
    mock_acompletion.return_value = make_text_response("No file tools due to error!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    # Processing should succeed even though storage raised
    assert response.reply_text == "No file tools due to error!"
    mock_get_storage.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_processing_failure_returns_fallback_reply(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When agent.process_message raises, a fallback reply is returned."""
    mock_acompletion.side_effect = RuntimeError("LLM service down")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert "trouble" in response.reply_text.lower()
    assert "try again" in response.reply_text.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_processing_failure_stores_fallback_outbound(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When agent fails, fallback reply is still stored as an outbound message."""
    mock_acompletion.side_effect = RuntimeError("LLM down")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert "trouble" in outbound.body.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_processing_failure_still_sends_reply(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When agent fails, the fallback reply is sent via messaging service."""
    mock_acompletion.side_effect = RuntimeError("LLM unavailable")  # type: ignore[union-attr]

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    mock_messaging.send_text.assert_called_once()  # type: ignore[union-attr]
    sent_body = mock_messaging.send_text.call_args.kwargs["body"]  # type: ignore[union-attr]
    assert "trouble" in sent_body.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_send_reply_failure_still_stores_outbound(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When send_text raises, outbound message is still persisted in DB."""
    mock_acompletion.return_value = make_text_response("Here is your reply!")  # type: ignore[union-attr]
    mock_messaging.send_text.side_effect = RuntimeError("Telegram API down")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    # The response is still produced
    assert response.reply_text == "Here is your reply!"
    # The outbound message is still stored despite send failure
    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == "Here is your reply!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.router.download_telegram_media", new_callable=AsyncMock)
async def test_pipeline_failure_without_downloaded_media_skips_vision_note(
    mock_download: AsyncMock,
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Pipeline failure with no downloaded media should NOT add vision note."""
    from backend.app.media.pipeline import PipelineResult

    # All downloads fail
    mock_download.side_effect = Exception("Download failed")
    mock_acompletion.return_value = make_text_response("Fallback!")  # type: ignore[union-attr]

    with patch(
        "backend.app.agent.router.process_message_media",
        new_callable=AsyncMock,
    ) as mock_pipeline:
        mock_pipeline.side_effect = [
            RuntimeError("Pipeline crashed"),
            PipelineResult(
                text_body=inbound_message.body,
                media_results=[],
                combined_context=f"[Text message]: '{inbound_message.body}'",
            ),
        ]

        await handle_inbound_message(
            db=db_session,
            contractor=test_contractor,
            message=inbound_message,
            media_urls=[("file_id_1", "image/jpeg")],
            messaging_service=mock_messaging,
        )

    db_session.refresh(inbound_message)
    # When downloaded_media is empty, we get the "couldn't download" note
    # but NOT the "Vision analysis was unavailable" note (that requires downloaded_media)
    assert "couldn't download" in inbound_message.processed_context.lower()
    assert "Vision analysis was unavailable" not in inbound_message.processed_context


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_empty_to_address_returns_early(
    mock_acompletion: object,
    db_session: Session,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """Contractor with no channel_identifier or phone should return early."""
    # Create contractor with empty delivery fields
    no_addr = Contractor(
        user_id="no-addr",
        channel_identifier="",
        phone="",
    )
    db_session.add(no_addr)
    db_session.commit()
    db_session.refresh(no_addr)

    response = await handle_inbound_message(
        db=db_session,
        contractor=no_addr,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    # Should return early without calling the LLM or sending any message
    assert response.reply_text == ""
    mock_acompletion.assert_not_called()  # type: ignore[union-attr]
    mock_messaging.send_text.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_send_media_reply_suppresses_duplicate_text(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """When agent calls send_media_reply, the router should NOT also send_text."""
    # LLM calls send_media_reply tool
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_media",
                "name": "send_media_reply",
                "arguments": json.dumps(
                    {"message": "Here's your file!", "media_url": "https://example.com/file.pdf"}
                ),
            }
        ]
    )
    # Follow-up LLM produces text that would duplicate the media reply
    followup_response = make_text_response("I've uploaded your photo!")

    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    # The router should detect send_media_reply and suppress the extra send_text
    mock_messaging.send_text.assert_not_called()  # type: ignore[union-attr]
    assert response.reply_text == "I've uploaded your photo!"


# ---------------------------------------------------------------------------
# Typed LLM exception handling in router (issue #173)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_content_filter_error_returns_rephrasing_message(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """ContentFilterError should produce a user-friendly rephrasing message."""
    mock_acompletion.side_effect = ContentFilterError("Blocked by safety filter")

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == CONTENT_FILTER_FALLBACK
    assert "rephrasing" in response.reply_text.lower()
    mock_messaging.send_text.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_authentication_error_returns_config_message(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """AuthenticationError should produce a configuration issue message."""
    mock_acompletion.side_effect = AuthenticationError("Invalid API key")

    response = await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    assert response.reply_text == AUTH_ERROR_FALLBACK
    assert "configuration" in response.reply_text.lower()
    mock_messaging.send_text.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_content_filter_error_stores_outbound_message(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """ContentFilterError fallback reply should be persisted as outbound message."""
    mock_acompletion.side_effect = ContentFilterError("Blocked")

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == CONTENT_FILTER_FALLBACK


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_authentication_error_stores_outbound_message(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
    inbound_message: Message,
    mock_messaging: MessagingService,
) -> None:
    """AuthenticationError fallback reply should be persisted as outbound message."""
    mock_acompletion.side_effect = AuthenticationError("Bad key")

    await handle_inbound_message(
        db=db_session,
        contractor=test_contractor,
        message=inbound_message,
        media_urls=[],
        messaging_service=mock_messaging,
    )

    outbound = db_session.query(Message).filter(Message.direction == "outbound").first()
    assert outbound is not None
    assert outbound.body == AUTH_ERROR_FALLBACK
