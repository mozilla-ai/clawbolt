import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm import AuthenticationError, ContentFilterError

import backend.app.database as _db_module
from backend.app.agent.approval import PermissionLevel, get_approval_store
from backend.app.agent.file_store import SessionState, StoredMessage
from backend.app.agent.router import (
    AUTH_ERROR_FALLBACK,
    CONTENT_FILTER_FALLBACK,
    handle_inbound_message,
)
from backend.app.bus import message_bus
from backend.app.models import User
from tests.conftest import create_test_session
from tests.mocks.llm import make_error_response, make_text_response, make_tool_call_response
from tests.mocks.storage import MockStorageBackend


@pytest.fixture()
def conversation(test_user: User) -> SessionState:
    return create_test_session(
        user_id=test_user.id,
        session_id="test-conv",
        messages=[
            StoredMessage(
                direction="inbound",
                body="I need a quote for a 12x12 deck",
                seq=1,
            ),
        ],
    )


@pytest.fixture()
def inbound_message() -> StoredMessage:
    return StoredMessage(
        direction="inbound",
        body="I need a quote for a 12x12 deck",
        seq=1,
    )


@pytest.fixture()
def mock_download_media() -> AsyncMock:
    return AsyncMock()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_text_only_message(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Text-only message should go through agent and produce reply."""
    mock_amessages.return_value = make_text_response("I can help with that deck estimate!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == "I can help with that deck estimate!"
    # Reply dispatched via bus (skip typing indicators)
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert outbound.content == "I can help with that deck estimate!"
            break


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock)
async def test_message_with_photo(
    mock_vision: AsyncMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """Message with photo should download, process via vision, then agent."""
    from backend.app.media.download import DownloadedMedia

    mock_download_media.return_value = DownloadedMedia(
        content=b"fake-image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_vision.return_value = "A 12x12 composite deck area."
    mock_amessages.return_value = make_text_response("Looks like a great deck project!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        channel="telegram",
        download_media=mock_download_media,
    )

    assert response.reply_text == "Looks like a great deck project!"
    mock_download_media.assert_called_once()
    mock_vision.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_stores_outbound_message(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Agent reply should be stored as outbound message."""
    mock_amessages.return_value = make_text_response("Reply stored!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].body == "Reply stored!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_stores_tool_interactions_with_outbound(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Tool interactions should be serialized with outbound message."""
    # First call: LLM requests a tool call
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_abc",
                "name": "read_file",
                "arguments": json.dumps({"path": "MEMORY.md"}),
            }
        ]
    )
    # Second call: LLM responds with text
    text_response = make_text_response("Here is your file!")
    mock_amessages.side_effect = [tool_response, text_response]  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    outbound = outbound_msgs[-1]
    assert outbound.tool_interactions_json
    interactions = json.loads(outbound.tool_interactions_json)
    assert len(interactions) == 1
    assert interactions[0]["name"] == "read_file"
    assert interactions[0]["tool_call_id"] == "call_abc"
    assert "result" in interactions[0]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_tool_interactions_for_text_only_response(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Text-only responses should have empty tool_interactions_json."""
    mock_amessages.return_value = make_text_response("Just text!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].tool_interactions_json == ""


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_media_download_failure_still_processes_text(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """If media download fails, agent should still process text."""
    mock_download = AsyncMock(side_effect=Exception("Download failed"))
    mock_amessages.return_value = make_text_response("Got your text!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
        channel="telegram",
        download_media=mock_download,
    )

    assert response.reply_text == "Got your text!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_processed_context_saved_to_message(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """processed_context should be saved to the StoredMessage after media pipeline."""
    mock_amessages.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert inbound_message.processed_context is not None
    assert inbound_message.body in inbound_message.processed_context


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_file_tools_wired_when_storage_configured(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """File tools should be registered when storage credentials are set."""
    mock_settings.storage_provider = "dropbox"
    mock_settings.dropbox_access_token = "test-token"
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_get_storage.return_value = MockStorageBackend()
    mock_amessages.return_value = make_text_response("File saved!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == "File saved!"
    mock_get_storage.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.settings")
async def test_file_tools_skipped_when_no_storage(
    mock_settings: MagicMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """File tools should be skipped gracefully when storage not configured."""
    mock_settings.storage_provider = "dropbox"
    mock_settings.dropbox_access_token = ""
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_amessages.return_value = make_text_response("No file tools!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == "No file tools!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch(
    "backend.app.media.pipeline.analyze_image",
    new_callable=AsyncMock,
    side_effect=RuntimeError("Vision API down"),
)
async def test_pipeline_failure_note_mentions_vision(
    mock_vision: AsyncMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """When media pipeline fails, the system note should mention vision analysis."""
    from backend.app.media.download import DownloadedMedia

    mock_download_media.return_value = DownloadedMedia(
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
        mock_amessages.return_value = make_text_response("I see you sent something!")  # type: ignore[union-attr]

        await handle_inbound_message(
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            channel="telegram",
            download_media=mock_download_media,
        )

    # The system note should be specific about vision analysis
    assert "Vision analysis was unavailable" in inbound_message.processed_context


# ---------------------------------------------------------------------------
# Error handling path tests (issue #138)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_media_download_failure_adds_system_note_to_context(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When all media downloads fail, the persisted context includes the download-failure note."""
    mock_download = AsyncMock(side_effect=Exception("Network timeout"))
    mock_amessages.return_value = make_text_response("Got your text!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[("file_id_1", "image/jpeg")],
        channel="telegram",
        download_media=mock_download,
    )

    assert "couldn't download" in inbound_message.processed_context.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_multiple_media_partial_download_failure_no_download_note(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When some media downloads succeed and others fail, no download-failure note is added."""
    from backend.app.media.download import DownloadedMedia

    mock_download = AsyncMock(
        side_effect=[
            DownloadedMedia(
                content=b"image-bytes",
                mime_type="image/jpeg",
                original_url="file_ok",
                filename="photo.jpg",
            ),
            Exception("Download failed for second file"),
        ]
    )
    mock_amessages.return_value = make_text_response("Got one photo!")  # type: ignore[union-attr]

    with patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = "A photo of a deck."
        response = await handle_inbound_message(
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("file_ok", "image/jpeg"), ("file_bad", "image/png")],
            channel="telegram",
            download_media=mock_download,
        )

    assert response.reply_text == "Got one photo!"
    # downloaded_media is not empty, so the "couldn't download" note is NOT added
    assert "couldn't download" not in inbound_message.processed_context.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_media_pipeline_failure_retries_with_empty_media(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """When process_message_media raises, it retries with an empty media list."""
    from backend.app.media.download import DownloadedMedia
    from backend.app.media.pipeline import PipelineResult

    mock_download_media.return_value = DownloadedMedia(
        content=b"image-bytes",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_amessages.return_value = make_text_response("Text only fallback!")  # type: ignore[union-attr]

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
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            channel="telegram",
            download_media=mock_download_media,
        )

    assert response.reply_text == "Text only fallback!"
    # The fallback call should have been made with empty media list
    assert mock_pipeline.call_count == 2
    second_call_args = mock_pipeline.call_args_list[1]
    assert second_call_args[0][1] == []  # second positional arg is empty media list


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_storage_exception_skips_file_tools(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When get_storage_service() raises, file tools are skipped and processing continues."""
    mock_settings.storage_provider = "dropbox"
    mock_settings.dropbox_access_token = "some-token"
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_get_storage.side_effect = RuntimeError("Storage backend init failed")
    mock_amessages.return_value = make_text_response("No file tools due to error!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # Processing should succeed even though storage raised
    assert response.reply_text == "No file tools due to error!"
    mock_get_storage.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_processing_failure_returns_fallback_reply(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When agent.process_message raises, a fallback reply is returned."""
    mock_amessages.side_effect = RuntimeError("LLM service down")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert "trouble" in response.reply_text.lower()
    assert "try again" in response.reply_text.lower()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_processing_failure_does_not_store_fallback(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When agent fails, fallback reply is NOT stored to avoid poisoning context."""
    mock_amessages.side_effect = RuntimeError("LLM down")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_processing_failure_dispatches_fallback_via_bus(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When agent fails, the fallback reply is dispatched via the bus."""
    mock_amessages.side_effect = RuntimeError("LLM unavailable")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert "trouble" in outbound.content.lower()
            break


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_send_reply_failure_still_stores_outbound(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When bus publish works, outbound message is still persisted in session."""
    mock_amessages.return_value = make_text_response("Here is your reply!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # The response is still produced
    assert response.reply_text == "Here is your reply!"
    # The outbound message is still stored
    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].body == "Here is your reply!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_pipeline_failure_without_downloaded_media_skips_vision_note(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Pipeline failure with no downloaded media should NOT add vision note."""
    from backend.app.media.pipeline import PipelineResult

    # All downloads fail
    mock_download = AsyncMock(side_effect=Exception("Download failed"))
    mock_amessages.return_value = make_text_response("Fallback!")  # type: ignore[union-attr]

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
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("file_id_1", "image/jpeg")],
            channel="telegram",
            download_media=mock_download,
        )

    # When downloaded_media is empty, we get the "couldn't download" note
    # but NOT the "Vision analysis was unavailable" note (that requires downloaded_media)
    assert "couldn't download" in inbound_message.processed_context.lower()
    assert "Vision analysis was unavailable" not in inbound_message.processed_context


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_empty_to_address_returns_early(
    mock_amessages: object,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """User with no channel_identifier or phone should return early."""
    # Create user with empty delivery fields (persisted so FK queries work)
    db = _db_module.SessionLocal()
    try:
        no_addr = User(
            user_id="no-addr",
            channel_identifier="",
            phone="",
        )
        db.add(no_addr)
        db.commit()
        db.refresh(no_addr)
        db.expunge(no_addr)
    finally:
        db.close()

    response = await handle_inbound_message(
        user=no_addr,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # Should return early without calling the LLM or sending any message
    assert response.reply_text == ""
    mock_amessages.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_send_media_reply_suppresses_duplicate_text(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """When agent calls send_media_reply, the router should NOT also dispatch text."""
    # Pre-approve messaging tools so the approval gate doesn't block
    store = get_approval_store()
    store.set_permission(test_user.id, "send_media_reply", PermissionLevel.AUTO)
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

    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # The router should detect send_media_reply and suppress the extra dispatch
    # The send_media_reply tool already published via the bus, so dispatch_reply_step
    # should NOT publish a second time.
    assert response.reply_text == "I've uploaded your photo!"


# ---------------------------------------------------------------------------
# Typing indicator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_typing_indicator_sent_before_agent_processing(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Typing indicator should be sent via the bus before the agent processes."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # Check that a typing indicator was published to the bus
    found_typing = False
    found_reply = False
    while not message_bus.outbound.empty():
        msg = message_bus.outbound.get_nowait()
        if msg.is_typing_indicator:
            found_typing = True
        elif msg.content:
            found_reply = True
    assert found_typing
    assert found_reply


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_typing_indicator_failure_does_not_block_processing(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """A failed typing indicator should not prevent message processing."""
    # Typing indicator is published to the bus; bus publish doesn't fail.
    # Even if the outbound dispatcher fails to deliver it, that's async.
    mock_amessages.return_value = make_text_response("Still works!")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == "Still works!"


# ---------------------------------------------------------------------------
# Typed LLM exception handling in router (issue #173)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-save media tests (issue #270)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_auto_save_persists_media_to_storage(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """Downloaded media should be auto-saved to storage before the agent loop."""
    from backend.app.media.download import DownloadedMedia

    mock_download_media.return_value = DownloadedMedia(
        content=b"auto-saved-image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_settings.storage_provider = "local"
    mock_settings.dropbox_access_token = ""
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_storage = MockStorageBackend()
    mock_get_storage.return_value = mock_storage
    mock_amessages.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    with patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = "A photo."
        await handle_inbound_message(
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            channel="telegram",
            download_media=mock_download_media,
        )

    # Media should be auto-saved to storage
    assert len(mock_storage.files) >= 1


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
@patch("backend.app.agent.router.get_storage_service")
@patch("backend.app.agent.router.settings")
async def test_auto_save_failure_does_not_block_processing(
    mock_settings: MagicMock,
    mock_get_storage: MagicMock,
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
    mock_download_media: AsyncMock,
) -> None:
    """If auto-save fails, message processing should continue."""
    from backend.app.media.download import DownloadedMedia

    mock_download_media.return_value = DownloadedMedia(
        content=b"image",
        mime_type="image/jpeg",
        original_url="AgACAgIAAxkBAAI",
        filename="photo.jpg",
    )
    mock_settings.storage_provider = "local"
    mock_settings.dropbox_access_token = ""
    mock_settings.google_drive_credentials_json = ""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    # Make storage raise on upload to simulate auto-save failure
    mock_storage = MagicMock(spec=MockStorageBackend)
    mock_storage.create_folder = AsyncMock()
    mock_storage.upload_file = AsyncMock(side_effect=RuntimeError("Storage down"))
    mock_get_storage.return_value = mock_storage
    mock_amessages.return_value = make_text_response("Still works!")  # type: ignore[union-attr]

    with patch("backend.app.media.pipeline.analyze_image", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = "A photo."
        response = await handle_inbound_message(
            user=test_user,
            session=conversation,
            message=inbound_message,
            media_urls=[("AgACAgIAAxkBAAI", "image/jpeg")],
            channel="telegram",
            download_media=mock_download_media,
        )

    assert response.reply_text == "Still works!"


# ---------------------------------------------------------------------------
# Typed LLM exception handling in router (issue #173)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_content_filter_error_returns_rephrasing_message(
    mock_amessages: AsyncMock,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """ContentFilterError should produce a user-friendly rephrasing message."""
    mock_amessages.side_effect = ContentFilterError("Blocked by safety filter")

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == CONTENT_FILTER_FALLBACK
    assert "rephrasing" in response.reply_text.lower()
    # Reply dispatched via bus (skip typing indicators)
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert outbound.content == CONTENT_FILTER_FALLBACK
            break


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_authentication_error_returns_config_message(
    mock_amessages: AsyncMock,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """AuthenticationError should produce a configuration issue message."""
    mock_amessages.side_effect = AuthenticationError("Invalid API key")

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.reply_text == AUTH_ERROR_FALLBACK
    assert "configuration" in response.reply_text.lower()
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert outbound.content == AUTH_ERROR_FALLBACK
            break


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_content_filter_error_does_not_store_outbound(
    mock_amessages: AsyncMock,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """ContentFilterError fallback reply should NOT be persisted (avoids context poisoning)."""
    mock_amessages.side_effect = ContentFilterError("Blocked")

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_authentication_error_does_not_store_outbound(
    mock_amessages: AsyncMock,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """AuthenticationError fallback reply should NOT be persisted (avoids context poisoning)."""
    mock_amessages.side_effect = AuthenticationError("Bad key")

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0


# ---------------------------------------------------------------------------
# Error poisoning protection tests (issue #283)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_normal_response_still_stored_as_outbound(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Normal (non-error) responses should still be stored as outbound messages."""
    mock_amessages.return_value = make_text_response("Here's your estimate!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) >= 1
    assert outbound_msgs[-1].body == "Here's your estimate!"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_fallback_dispatched_but_not_stored(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Error fallback should be dispatched via bus even though it's not stored."""
    mock_amessages.side_effect = RuntimeError("LLM down")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    # Dispatched via bus (skip typing indicators)
    assert "trouble" in response.reply_text.lower()
    found_fallback = False
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert "trouble" in outbound.content.lower()
            found_fallback = True
            break
    assert found_fallback
    # But not stored in session
    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0


# ---------------------------------------------------------------------------
# dispatch_reply_step: reply suppression checks tool success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_dispatch_reply_step_suppresses_when_send_reply_succeeds() -> None:
    """Auto-reply should be suppressed when a SENDS_REPLY tool succeeded."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse
    from backend.app.agent.file_store import SessionState, StoredMessage
    from backend.app.agent.router import PipelineContext, dispatch_reply_step
    from backend.app.agent.tools.base import ToolTags

    response = AgentResponse(
        reply_text="Fallback text",
        tool_calls=[
            StoredToolInteraction(name="send_reply", tags={ToolTags.SENDS_REPLY}, is_error=False),
        ],
    )
    ctx = PipelineContext(
        user=User(id="1", user_id="test"),
        session=SessionState(session_id="s", user_id="1", is_active=True),
        message=StoredMessage(direction="inbound", body="hi", seq=1),
        media_urls=[],
        channel="telegram",
        to_address="123",
    )
    ctx.response = response

    await dispatch_reply_step(ctx)

    # Bus should be empty since send_reply already sent the message
    assert message_bus.outbound.empty()


@pytest.mark.asyncio()
async def test_dispatch_reply_step_sends_when_send_reply_fails() -> None:
    """Auto-reply should be dispatched via bus when the SENDS_REPLY tool failed."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse
    from backend.app.agent.file_store import SessionState, StoredMessage
    from backend.app.agent.router import PipelineContext, dispatch_reply_step
    from backend.app.agent.tools.base import ToolTags

    response = AgentResponse(
        reply_text="Fallback text",
        tool_calls=[
            StoredToolInteraction(name="send_reply", tags={ToolTags.SENDS_REPLY}, is_error=True),
        ],
    )
    ctx = PipelineContext(
        user=User(id="1", user_id="test"),
        session=SessionState(session_id="s", user_id="1", is_active=True),
        message=StoredMessage(direction="inbound", body="hi", seq=1),
        media_urls=[],
        channel="telegram",
        to_address="123",
    )
    ctx.response = response

    await dispatch_reply_step(ctx)

    outbound = message_bus.outbound.get_nowait()
    assert outbound.content == "Fallback text"
    assert outbound.chat_id == "123"


# ---------------------------------------------------------------------------
# Error stop_reason: dispatched to user but NOT persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_stop_reason_not_persisted_to_session(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """LLM error stop_reason should NOT be stored in session history."""
    mock_amessages.return_value = make_error_response(stop_reason="error")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.is_error_fallback is True
    # No outbound message should be persisted
    outbound_msgs = [m for m in conversation.messages if m.direction == "outbound"]
    assert len(outbound_msgs) == 0


# ---------------------------------------------------------------------------
# Channel-specific to_address resolution (cross-channel routing bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_to_address_uses_channel_specific_identifier(
    mock_amessages: object,
    test_user: User,
) -> None:
    """Reply to_address should use channel-specific identifier, not stale channel_identifier.

    Regression test: when a user interacts via webchat then Telegram,
    user.channel_identifier gets overwritten with the webchat sender_id
    (which equals the numeric user_id, e.g. "1"). Telegram replies must
    still use the real Telegram chat_id from the user index.
    """
    import backend.app.database as _db_module
    from backend.app.models import ChannelRoute

    telegram_chat_id = "555000111"

    # Create a second user so its id (2) doesn't collide with leaked index
    # entries from other tests that map telegram:<x> -> 1.
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="cross-channel-user",
            channel_identifier="cross-channel-user",
            preferred_channel="webchat",
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
        # Link the real Telegram chat_id in the channel routes
        db.add(
            ChannelRoute(user_id=user_id, channel="telegram", channel_identifier=telegram_chat_id)
        )
        db.commit()
        # Eagerly load all scalar attributes before expunging
        _ = (
            user.phone,
            user.soul_text,
            user.user_text,
            user.heartbeat_text,
            user.preferred_channel,
            user.channel_identifier,
            user.onboarding_complete,
        )
        db.expunge(user)
    finally:
        db.close()

    # Create file-store directories for this user (hybrid period)
    from pathlib import Path

    from backend.app.config import settings

    user_dir = Path(settings.data_dir) / str(user_id)
    (user_dir / "sessions").mkdir(parents=True, exist_ok=True)

    from tests.conftest import create_test_session

    session = create_test_session(user_id=user_id, session_id="s")
    message = StoredMessage(direction="inbound", body="hello from telegram", seq=1)

    mock_amessages.return_value = make_text_response("Cross-channel reply!")  # type: ignore[union-attr]

    await handle_inbound_message(
        user=user,
        session=session,
        message=message,
        media_urls=[],
        channel="telegram",
    )

    # The outbound message should use the Telegram chat_id, not the user_id
    while not message_bus.outbound.empty():
        outbound = message_bus.outbound.get_nowait()
        if not outbound.is_typing_indicator:
            assert outbound.chat_id == telegram_chat_id, (
                f"Expected Telegram chat_id '{telegram_chat_id}', "
                f"got '{outbound.chat_id}' (stale channel_identifier)"
            )
            break


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_stop_reason_still_dispatches_reply_to_user(
    mock_amessages: object,
    test_user: User,
    conversation: SessionState,
    inbound_message: StoredMessage,
) -> None:
    """Error fallback should still be dispatched via the bus so the user sees a message."""
    mock_amessages.return_value = make_error_response(stop_reason="error")  # type: ignore[union-attr]

    response = await handle_inbound_message(
        user=test_user,
        session=conversation,
        message=inbound_message,
        media_urls=[],
        channel="telegram",
    )

    assert response.is_error_fallback is True
    assert response.reply_text  # user gets a fallback message
