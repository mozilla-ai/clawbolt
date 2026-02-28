from unittest.mock import MagicMock

import pytest

from backend.app.agent.tools.twilio_tools import create_twilio_tools
from backend.app.services.twilio_service import TwilioService


@pytest.fixture()
def mock_twilio_service() -> TwilioService:
    service = TwilioService.__new__(TwilioService)
    service.client = MagicMock()
    service.from_number = "+15559876543"
    mock_msg = MagicMock()
    mock_msg.sid = "SM_test_sid"
    service.client.messages.create.return_value = mock_msg
    return service


@pytest.mark.asyncio()
async def test_send_reply_tool(mock_twilio_service: TwilioService) -> None:
    """send_reply tool should send SMS and return SID."""
    tools = create_twilio_tools(mock_twilio_service, to_number="+15551234567")
    send_reply = tools[0].function
    result = await send_reply(message="Your estimate is ready!")
    assert "SM_test_sid" in result
    mock_twilio_service.client.messages.create.assert_called_once()


@pytest.mark.asyncio()
async def test_send_media_reply_tool(mock_twilio_service: TwilioService) -> None:
    """send_media_reply tool should send MMS with media URL."""
    tools = create_twilio_tools(mock_twilio_service, to_number="+15551234567")
    send_media_reply = tools[1].function
    result = await send_media_reply(
        message="Here's your estimate", media_url="https://example.com/estimate.pdf"
    )
    assert "SM_test_sid" in result
