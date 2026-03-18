from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.app.agent.tools.messaging_tools import create_messaging_tools
from backend.app.bus import OutboundMessage


@pytest.fixture()
def publish_outbound() -> AsyncMock:
    return AsyncMock()


@pytest.mark.asyncio()
async def test_send_reply_tool(publish_outbound: AsyncMock) -> None:
    """send_reply tool should publish an OutboundMessage and return confirmation."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="Your estimate is ready!")
    assert "Sent message" in result.content
    assert result.is_error is False
    publish_outbound.assert_called_once()
    msg: OutboundMessage = publish_outbound.call_args[0][0]
    assert msg.chat_id == "123456789"
    assert msg.content == "Your estimate is ready!"
    assert msg.channel == "telegram"


@pytest.mark.asyncio()
async def test_send_reply_rejects_empty_message(publish_outbound: AsyncMock) -> None:
    """send_reply should return error for empty messages."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="")
    assert "Error" in result.content
    assert result.is_error is True
    publish_outbound.assert_not_called()


@pytest.mark.asyncio()
async def test_send_reply_rejects_whitespace_message(
    publish_outbound: AsyncMock,
) -> None:
    """send_reply should return error for whitespace-only messages."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_reply = tools[0].function
    result = await send_reply(message="   ")
    assert "Error" in result.content
    assert result.is_error is True
    publish_outbound.assert_not_called()


@pytest.mark.asyncio()
async def test_send_media_reply_rejects_empty_url(
    publish_outbound: AsyncMock,
) -> None:
    """send_media_reply should return error for empty media_url."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(message="Here's your file", media_url="")
    assert "Error" in result.content
    assert result.is_error is True
    publish_outbound.assert_not_called()


@pytest.mark.asyncio()
async def test_send_media_reply_tool(publish_outbound: AsyncMock) -> None:
    """send_media_reply tool should publish an OutboundMessage with media."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(
        message="Here's your estimate", media_url="https://example.com/estimate.pdf"
    )
    assert "Sent media message" in result.content
    assert result.is_error is False
    publish_outbound.assert_called_once()
    msg: OutboundMessage = publish_outbound.call_args[0][0]
    assert msg.chat_id == "123456789"
    assert msg.content == "Here's your estimate"
    assert msg.media == ["https://example.com/estimate.pdf"]
    assert msg.channel == "telegram"


@pytest.mark.asyncio()
async def test_send_media_reply_accepts_local_file(
    publish_outbound: AsyncMock,
    tmp_path: Path,
) -> None:
    """send_media_reply should accept a local file path that exists."""
    pdf = tmp_path / "estimate.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")

    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(message="Here's your estimate", media_url=str(pdf))
    assert result.is_error is False
    assert "Sent media message" in result.content
    publish_outbound.assert_called_once()


@pytest.mark.asyncio()
async def test_send_media_reply_rejects_invalid_url(publish_outbound: AsyncMock) -> None:
    """send_media_reply should reject a URL without protocol that isn't a local file."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(
        message="Here's your file",
        media_url="data/estimates/nonexistent/EST-0001.pdf",
    )
    assert result.is_error is True
    assert "not a valid URL" in result.content
    publish_outbound.assert_not_called()


@pytest.mark.asyncio()
async def test_send_media_reply_rejects_bare_domain(publish_outbound: AsyncMock) -> None:
    """send_media_reply should reject a URL like 'example.com/file.pdf' (no protocol)."""
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="123456789")
    send_media_reply = tools[1].function
    result = await send_media_reply(
        message="Here's your file",
        media_url="example.com/estimate.pdf",
    )
    assert result.is_error is True
    assert "not a valid URL" in result.content
    publish_outbound.assert_not_called()
