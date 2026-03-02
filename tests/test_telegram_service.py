from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.telegram_service import TelegramMessagingService

# ---------------------------------------------------------------------------
# _parse_chat_id
# ---------------------------------------------------------------------------


class TestParseChatId:
    def test_plain_numeric(self) -> None:
        assert TelegramMessagingService._parse_chat_id("123456789") == 123456789

    def test_strips_plus_prefix(self) -> None:
        assert TelegramMessagingService._parse_chat_id("+15551234567") == 15551234567

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Telegram chat_id"):
            TelegramMessagingService._parse_chat_id("")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Telegram chat_id"):
            TelegramMessagingService._parse_chat_id("not-a-number")


@pytest.fixture()
def mock_bot() -> MagicMock:
    """Create a mock Telegram Bot."""
    bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 42
    bot.send_message = AsyncMock(return_value=mock_msg)
    bot.send_photo = AsyncMock(return_value=mock_msg)
    bot.send_document = AsyncMock(return_value=mock_msg)
    bot.send_chat_action = AsyncMock()
    return bot


@pytest.fixture()
def telegram_service(mock_bot: MagicMock) -> TelegramMessagingService:
    """Create a TelegramMessagingService with mocked Bot."""
    service = TelegramMessagingService.__new__(TelegramMessagingService)
    service.bot = mock_bot
    service._token = "test-token"
    return service


@pytest.mark.asyncio()
async def test_send_text(telegram_service: TelegramMessagingService, mock_bot: MagicMock) -> None:
    """send_text should call bot.send_message with correct params."""
    msg_id = await telegram_service.send_text(to="123456789", body="Your estimate is ready")
    assert msg_id == "42"
    mock_bot.send_message.assert_called_once_with(chat_id=123456789, text="Your estimate is ready")


@pytest.mark.asyncio()
@patch("backend.app.services.telegram_service.httpx.AsyncClient")
async def test_send_media_image(
    mock_client_class: MagicMock,
    telegram_service: TelegramMessagingService,
    mock_bot: MagicMock,
) -> None:
    """send_media with an image URL should call bot.send_photo."""
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.content = b"fake-image-data"
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_class.return_value = mock_client

    msg_id = await telegram_service.send_media(
        to="123456789",
        body="Here is the photo",
        media_url="https://example.com/photo.jpg",
    )
    assert msg_id == "42"
    mock_bot.send_photo.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.services.telegram_service.httpx.AsyncClient")
async def test_send_media_document(
    mock_client_class: MagicMock,
    telegram_service: TelegramMessagingService,
    mock_bot: MagicMock,
) -> None:
    """send_media with a PDF URL should call bot.send_document."""
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "application/pdf"}
    mock_response.content = b"fake-pdf-data"
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_class.return_value = mock_client

    msg_id = await telegram_service.send_media(
        to="123456789",
        body="Here is the PDF",
        media_url="https://example.com/estimate.pdf",
    )
    assert msg_id == "42"
    mock_bot.send_document.assert_called_once()


@pytest.mark.asyncio()
async def test_send_message_text_only(
    telegram_service: TelegramMessagingService, mock_bot: MagicMock
) -> None:
    """send_message without media_urls should send text."""
    msg_id = await telegram_service.send_message(to="123456789", body="Hello")
    assert msg_id == "42"
    mock_bot.send_message.assert_called_once_with(chat_id=123456789, text="Hello")


@pytest.mark.asyncio()
@patch("backend.app.services.telegram_service.httpx.AsyncClient")
async def test_send_message_multi_media_caption_once(
    mock_client_class: MagicMock,
    telegram_service: TelegramMessagingService,
    mock_bot: MagicMock,
) -> None:
    """send_message with multiple media URLs should only caption the first."""
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.content = b"fake-image-data"
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_class.return_value = mock_client

    await telegram_service.send_message(
        to="123456789",
        body="Here are the photos",
        media_urls=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )

    calls = mock_bot.send_photo.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["caption"] == "Here are the photos"
    assert calls[1].kwargs["caption"] == ""


# ---------------------------------------------------------------------------
# send_typing_indicator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_typing_indicator(
    telegram_service: TelegramMessagingService, mock_bot: MagicMock
) -> None:
    """send_typing_indicator should call bot.send_chat_action with 'typing'."""
    from telegram.constants import ChatAction

    await telegram_service.send_typing_indicator(to="123456789")
    mock_bot.send_chat_action.assert_called_once_with(chat_id=123456789, action=ChatAction.TYPING)


@pytest.mark.asyncio()
async def test_send_typing_indicator_failure_does_not_raise(
    telegram_service: TelegramMessagingService, mock_bot: MagicMock
) -> None:
    """send_typing_indicator should swallow exceptions and not raise."""
    mock_bot.send_chat_action.side_effect = RuntimeError("Telegram API error")
    # Should not raise
    await telegram_service.send_typing_indicator(to="123456789")
