from unittest.mock import MagicMock

import pytest

from backend.app.services.twilio_service import TwilioService


@pytest.fixture()
def mock_twilio_client() -> MagicMock:
    """Create a mock Twilio client."""
    client = MagicMock()
    mock_message = MagicMock()
    mock_message.sid = "SM_test_message_sid"
    client.messages.create.return_value = mock_message
    return client


@pytest.fixture()
def twilio_service(mock_twilio_client: MagicMock) -> TwilioService:
    """Create a TwilioService with mocked client."""
    service = TwilioService.__new__(TwilioService)
    service.client = mock_twilio_client
    service.from_number = "+15559876543"
    return service


@pytest.mark.asyncio()
async def test_send_sms(twilio_service: TwilioService, mock_twilio_client: MagicMock) -> None:
    """send_sms should call messages.create with correct params."""
    sid = await twilio_service.send_sms(to="+15551234567", body="Your estimate is ready")
    assert sid == "SM_test_message_sid"
    mock_twilio_client.messages.create.assert_called_once_with(
        to="+15551234567",
        from_="+15559876543",
        body="Your estimate is ready",
    )


@pytest.mark.asyncio()
async def test_send_mms(twilio_service: TwilioService, mock_twilio_client: MagicMock) -> None:
    """send_mms should include media_url parameter."""
    sid = await twilio_service.send_mms(
        to="+15551234567",
        body="Here is the PDF",
        media_url="https://example.com/estimate.pdf",
    )
    assert sid == "SM_test_message_sid"
    mock_twilio_client.messages.create.assert_called_once_with(
        to="+15551234567",
        from_="+15559876543",
        body="Here is the PDF",
        media_url=["https://example.com/estimate.pdf"],
    )


@pytest.mark.asyncio()
async def test_send_message_sms(
    twilio_service: TwilioService, mock_twilio_client: MagicMock
) -> None:
    """send_message without media_urls should send SMS."""
    sid = await twilio_service.send_message(to="+15551234567", body="Hello")
    assert sid == "SM_test_message_sid"
    mock_twilio_client.messages.create.assert_called_once_with(
        to="+15551234567",
        from_="+15559876543",
        body="Hello",
    )


@pytest.mark.asyncio()
async def test_send_message_mms(
    twilio_service: TwilioService, mock_twilio_client: MagicMock
) -> None:
    """send_message with media_urls should send MMS."""
    sid = await twilio_service.send_message(
        to="+15551234567",
        body="Photos attached",
        media_urls=["https://example.com/photo1.jpg"],
    )
    assert sid == "SM_test_message_sid"
    mock_twilio_client.messages.create.assert_called_once_with(
        to="+15551234567",
        from_="+15559876543",
        body="Photos attached",
        media_url=["https://example.com/photo1.jpg"],
    )
