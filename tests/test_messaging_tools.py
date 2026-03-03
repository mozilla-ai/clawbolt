from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.agent.tools.messaging_tools import create_messaging_tools
from backend.app.services.messaging import MessagingService


@pytest.fixture()
def mock_messaging_service() -> MessagingService:
    service = MagicMock(spec=MessagingService)
    service.send_text = AsyncMock(return_value="msg_42")
    service.send_media = AsyncMock(return_value="msg_43")
    service.download_media = AsyncMock()
    return service


@pytest.mark.asyncio()
async def test_send_reply_tool(mock_messaging_service: MessagingService) -> None:
    """send_reply tool should send text and return message ID."""
    tools = create_messaging_tools(mock_messaging_service, to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="Your estimate is ready!")
    assert "msg_42" in result.content
    assert result.is_error is False
    mock_messaging_service.send_text.assert_called_once_with(  # type: ignore[union-attr]
        to="123456789", body="Your estimate is ready!"
    )


@pytest.mark.asyncio()
async def test_send_reply_rejects_empty_message(mock_messaging_service: MessagingService) -> None:
    """send_reply should return error for empty messages."""
    tools = create_messaging_tools(mock_messaging_service, to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="")
    assert "Error" in result.content
    assert result.is_error is True
    mock_messaging_service.send_text.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_send_reply_rejects_whitespace_message(
    mock_messaging_service: MessagingService,
) -> None:
    """send_reply should return error for whitespace-only messages."""
    tools = create_messaging_tools(mock_messaging_service, to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="   ")
    assert "Error" in result.content
    assert result.is_error is True
    mock_messaging_service.send_text.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_send_media_reply_rejects_empty_url(
    mock_messaging_service: MessagingService,
) -> None:
    """send_media_reply should return error for empty media_url."""
    tools = create_messaging_tools(mock_messaging_service, to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(message="Here's your file", media_url="")
    assert "Error" in result.content
    assert result.is_error is True
    mock_messaging_service.send_media.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio()
async def test_send_media_reply_tool(mock_messaging_service: MessagingService) -> None:
    """send_media_reply tool should send media with message."""
    tools = create_messaging_tools(mock_messaging_service, to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(
        message="Here's your estimate", media_url="https://example.com/estimate.pdf"
    )
    assert "msg_43" in result.content
    assert result.is_error is False
    mock_messaging_service.send_media.assert_called_once_with(  # type: ignore[union-attr]
        to="123456789", body="Here's your estimate", media_url="https://example.com/estimate.pdf"
    )
