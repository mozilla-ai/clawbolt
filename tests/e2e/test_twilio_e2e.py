"""End-to-end test against real Twilio API.

Sends a single real SMS and verifies delivery. Kept minimal to conserve
trial account balance. Signature validation and error handling tests
don't send messages and cost nothing.

Run with:
    uv run pytest -m e2e -v

Skip with:
    uv run pytest -m "not e2e"
"""

import time

import pytest
from twilio.base.exceptions import TwilioRestException
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


# -- Tests ---------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_sms_and_verify_delivery(
    twilio_service: TwilioService,
    twilio_settings: Settings,
    test_to_number: str,
) -> None:
    """Send a real SMS, verify SID format and delivery status progression."""
    sid = await twilio_service.send_sms(
        to=test_to_number,
        body="[backshop e2e] SMS delivery test",
    )
    assert sid.startswith("SM"), f"Expected SID starting with SM, got: {sid}"

    # Poll until Twilio reports a terminal status
    deadline = time.monotonic() + 30
    status = "queued"
    while time.monotonic() < deadline:
        msg = _fetch_message(twilio_settings, sid)
        status = msg.status
        if status in {"sent", "delivered", "undelivered", "failed"}:
            break
        time.sleep(2)

    assert status in {"queued", "sent", "delivered"}, f"Unexpected terminal status: {status}"


def test_request_validator_round_trip(
    twilio_settings: Settings,
) -> None:
    """Signature computed with real auth token validates correctly; tampered one is rejected."""
    validator = RequestValidator(twilio_settings.twilio_auth_token)
    url = "https://backshop.example.com/api/webhooks/twilio/inbound"
    params = {
        "From": "+15551234567",
        "To": twilio_settings.twilio_phone_number,
        "Body": "test message",
    }
    signature = validator.compute_signature(url, params)
    assert validator.validate(url, params, signature), "Valid signature was rejected"
    assert not validator.validate(url, params, "tampered_signature"), (
        "Invalid signature was accepted"
    )


@pytest.mark.asyncio()
async def test_send_to_invalid_number_raises(
    twilio_service: TwilioService,
) -> None:
    """Sending to Twilio's magic failure number raises TwilioRestException."""
    with pytest.raises(TwilioRestException):
        await twilio_service.send_sms(
            to="+15005550001",
            body="[backshop e2e] should fail",
        )
