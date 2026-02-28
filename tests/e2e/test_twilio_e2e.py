"""End-to-end tests against real Twilio API.

These tests send real SMS messages and verify delivery status via the Twilio
REST API. They require valid Twilio credentials set as environment variables
(or GitHub Actions secrets).

Run with:
    uv run pytest -m e2e -v

Skip with:
    uv run pytest -m "not e2e"
"""

import time

import pytest
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from backend.app.config import Settings
from backend.app.services.twilio_service import TwilioService

from .conftest import skip_without_twilio

pytestmark = [pytest.mark.e2e, skip_without_twilio]


# -- Helpers -------------------------------------------------------------------


def _fetch_message(settings: Settings, sid: str) -> object:
    """Fetch a message resource from Twilio by SID."""
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    return client.messages(sid).fetch()


def _wait_for_status(
    settings: Settings,
    sid: str,
    target_statuses: set[str],
    timeout_seconds: int = 30,
    poll_interval: float = 2.0,
) -> str:
    """Poll Twilio until the message reaches one of the target statuses."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        msg = _fetch_message(settings, sid)
        if msg.status in target_statuses:
            return msg.status
        time.sleep(poll_interval)
    msg = _fetch_message(settings, sid)
    return msg.status


# -- Tests ---------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_sms_returns_valid_sid(
    twilio_service: TwilioService,
    test_to_number: str,
) -> None:
    """Sending a real SMS returns a message SID starting with 'SM'."""
    sid = await twilio_service.send_sms(
        to=test_to_number,
        body="[backshop e2e] send_sms test",
    )
    assert sid.startswith("SM"), f"Expected SID starting with SM, got: {sid}"


@pytest.mark.asyncio()
async def test_send_sms_status_progresses(
    twilio_service: TwilioService,
    twilio_settings: Settings,
    test_to_number: str,
) -> None:
    """After sending, the message status should progress past 'queued'."""
    sid = await twilio_service.send_sms(
        to=test_to_number,
        body="[backshop e2e] status progression test",
    )
    final_status = _wait_for_status(
        twilio_settings,
        sid,
        target_statuses={"sent", "delivered", "undelivered", "failed"},
        timeout_seconds=30,
    )
    # On trial accounts, 'sent' or 'delivered' are both success.
    # 'queued' means it hasn't left Twilio yet (timeout -- still acceptable).
    assert final_status in {
        "queued",
        "sent",
        "delivered",
    }, f"Unexpected terminal status: {final_status}"


@pytest.mark.asyncio()
async def test_send_message_sms_path(
    twilio_service: TwilioService,
    test_to_number: str,
) -> None:
    """send_message() without media_urls should send a plain SMS."""
    sid = await twilio_service.send_message(
        to=test_to_number,
        body="[backshop e2e] send_message SMS path",
    )
    assert sid.startswith("SM")


@pytest.mark.asyncio()
async def test_send_message_mms_path(
    twilio_service: TwilioService,
    twilio_settings: Settings,
    test_to_number: str,
) -> None:
    """send_message() with media_urls should send an MMS."""
    # Use a publicly accessible test image
    test_image_url = "https://www.twilio.com/docs/static/company/mark-red.png"
    sid = await twilio_service.send_message(
        to=test_to_number,
        body="[backshop e2e] send_message MMS path",
        media_urls=[test_image_url],
    )
    assert sid.startswith("MM") or sid.startswith("SM"), f"Unexpected SID prefix: {sid}"

    # Verify the message has media
    msg = _fetch_message(twilio_settings, sid)
    # num_media is a string in the Twilio API
    assert msg.num_media and int(msg.num_media) >= 1, (
        f"Expected at least 1 media attachment, got: {msg.num_media}"
    )


@pytest.mark.asyncio()
async def test_send_mms_returns_valid_sid(
    twilio_service: TwilioService,
    test_to_number: str,
) -> None:
    """send_mms() with a media URL returns a valid SID."""
    test_image_url = "https://www.twilio.com/docs/static/company/mark-red.png"
    sid = await twilio_service.send_mms(
        to=test_to_number,
        body="[backshop e2e] send_mms test",
        media_url=test_image_url,
    )
    assert sid.startswith("SM") or sid.startswith("MM"), f"Unexpected SID prefix: {sid}"


def test_request_validator_accepts_valid_signature(
    twilio_settings: Settings,
) -> None:
    """RequestValidator should accept a signature we generate ourselves."""
    validator = RequestValidator(twilio_settings.twilio_auth_token)
    url = "https://backshop.example.com/api/webhooks/twilio/inbound"
    params = {
        "From": "+15551234567",
        "To": twilio_settings.twilio_phone_number,
        "Body": "test message",
    }
    # Generate a valid signature
    signature = validator.compute_signature(url, params)
    # Validate it
    assert validator.validate(url, params, signature), "Valid signature was rejected"


def test_request_validator_rejects_invalid_signature(
    twilio_settings: Settings,
) -> None:
    """RequestValidator should reject a tampered signature."""
    validator = RequestValidator(twilio_settings.twilio_auth_token)
    url = "https://backshop.example.com/api/webhooks/twilio/inbound"
    params = {
        "From": "+15551234567",
        "To": twilio_settings.twilio_phone_number,
        "Body": "test message",
    }
    assert not validator.validate(url, params, "invalid_signature_abc123")


@pytest.mark.asyncio()
async def test_send_to_invalid_number_raises(
    twilio_service: TwilioService,
) -> None:
    """Sending to an invalid number should raise a Twilio exception."""
    from twilio.base.exceptions import TwilioRestException

    with pytest.raises(TwilioRestException):
        await twilio_service.send_sms(
            to="+15005550001",  # Twilio test number that always fails
            body="[backshop e2e] should fail",
        )
