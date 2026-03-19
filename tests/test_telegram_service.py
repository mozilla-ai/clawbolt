from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.channels.telegram import TelegramChannel, markdown_to_telegram_mdv2

# ---------------------------------------------------------------------------
# _parse_chat_id
# ---------------------------------------------------------------------------


class TestParseChatId:
    def test_plain_numeric(self) -> None:
        assert TelegramChannel._parse_chat_id("123456789") == 123456789

    def test_strips_plus_prefix(self) -> None:
        assert TelegramChannel._parse_chat_id("+15551234567") == 15551234567

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Telegram chat_id"):
            TelegramChannel._parse_chat_id("")

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid Telegram chat_id"):
            TelegramChannel._parse_chat_id("not-a-number")


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
def telegram_service(mock_bot: MagicMock) -> TelegramChannel:
    """Create a TelegramChannel with mocked Bot."""
    service = TelegramChannel.__new__(TelegramChannel)
    service.bot = mock_bot
    service._token = "test-token"
    return service


@pytest.mark.asyncio()
async def test_send_text(telegram_service: TelegramChannel, mock_bot: MagicMock) -> None:
    """send_text should call bot.send_message with MarkdownV2."""
    msg_id = await telegram_service.send_text(to="123456789", body="Your estimate is ready")
    assert msg_id == "42"
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == 123456789
    assert call_kwargs["parse_mode"] == "MarkdownV2"
    assert call_kwargs["text"] == markdown_to_telegram_mdv2("Your estimate is ready")


@pytest.mark.asyncio()
@patch("backend.app.channels.telegram.httpx.AsyncClient")
async def test_send_media_image(
    mock_client_class: MagicMock,
    telegram_service: TelegramChannel,
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
@patch("backend.app.channels.telegram.httpx.AsyncClient")
async def test_send_media_document(
    mock_client_class: MagicMock,
    telegram_service: TelegramChannel,
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
async def test_send_media_rejects_invalid_url(
    telegram_service: TelegramChannel,
) -> None:
    """send_media should raise ValueError for a URL without protocol that isn't a local file."""
    with pytest.raises(ValueError, match="not a reachable local file"):
        await telegram_service.send_media(
            to="123456789",
            body="Here is the file",
            media_url="data/estimates/nonexistent/EST-0001.pdf",
        )


@pytest.mark.asyncio()
async def test_send_message_text_only(
    telegram_service: TelegramChannel, mock_bot: MagicMock
) -> None:
    """send_message without media_urls should send text."""
    msg_id = await telegram_service.send_message(to="123456789", body="Hello")
    assert msg_id == "42"
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == 123456789
    assert call_kwargs["parse_mode"] == "MarkdownV2"
    assert call_kwargs["text"] == markdown_to_telegram_mdv2("Hello")


@pytest.mark.asyncio()
@patch("backend.app.channels.telegram.httpx.AsyncClient")
async def test_send_message_multi_media_caption_once(
    mock_client_class: MagicMock,
    telegram_service: TelegramChannel,
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
    telegram_service: TelegramChannel, mock_bot: MagicMock
) -> None:
    """send_typing_indicator should call bot.send_chat_action with 'typing'."""
    from telegram.constants import ChatAction

    await telegram_service.send_typing_indicator(to="123456789")
    mock_bot.send_chat_action.assert_called_once_with(chat_id=123456789, action=ChatAction.TYPING)


@pytest.mark.asyncio()
async def test_send_typing_indicator_failure_does_not_raise(
    telegram_service: TelegramChannel, mock_bot: MagicMock
) -> None:
    """send_typing_indicator should swallow exceptions and not raise."""
    mock_bot.send_chat_action.side_effect = RuntimeError("Telegram API error")
    # Should not raise
    await telegram_service.send_typing_indicator(to="123456789")


# ---------------------------------------------------------------------------
# markdown_to_telegram_mdv2
# ---------------------------------------------------------------------------


class TestMarkdownToTelegramMdv2:
    def test_bold(self) -> None:
        assert markdown_to_telegram_mdv2("**hello**") == "*hello*"

    def test_italic(self) -> None:
        assert markdown_to_telegram_mdv2("*hello*") == "_hello_"

    def test_inline_code(self) -> None:
        assert markdown_to_telegram_mdv2("`foo()`") == "`foo()`"

    def test_fenced_code_block(self) -> None:
        md = "```python\nprint('hi')\n```"
        result = markdown_to_telegram_mdv2(md)
        assert result.startswith("```python\n")
        assert result.endswith("\n```")
        assert "print('hi')" in result

    def test_link(self) -> None:
        assert markdown_to_telegram_mdv2("[click](https://x.com)") == "[click](https://x.com)"

    def test_heading_becomes_bold(self) -> None:
        result = markdown_to_telegram_mdv2("## My heading")
        assert result == "*My heading*"

    def test_special_chars_escaped(self) -> None:
        result = markdown_to_telegram_mdv2("Price is $50.00!")
        assert "\\." in result
        assert "\\!" in result

    def test_plain_text_no_special_chars(self) -> None:
        assert markdown_to_telegram_mdv2("just text") == "just text"

    def test_mixed_formatting(self) -> None:
        result = markdown_to_telegram_mdv2("**bold** and *italic* and `code`")
        assert "*bold*" in result
        assert "_italic_" in result
        assert "`code`" in result

    def test_code_block_no_over_escaping(self) -> None:
        """Special chars inside code blocks should not be escaped."""
        md = "```\nx = 1 + 2\n```"
        result = markdown_to_telegram_mdv2(md)
        # + should NOT be escaped inside code blocks
        assert "\\+" not in result
        assert "x = 1 + 2" in result

    def test_url_parens_escaped(self) -> None:
        result = markdown_to_telegram_mdv2("[text](https://x.com/a_(b))")
        assert "\\)" in result
