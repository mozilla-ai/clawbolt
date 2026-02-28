"""Shared fixtures for e2e tests that hit real external services."""

import os

import pytest

from backend.app.config import Settings
from backend.app.services.twilio_service import TwilioService


def _twilio_credentials_available() -> bool:
    """Check if all required Twilio env vars are set."""
    return all(
        os.environ.get(var)
        for var in [
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_PHONE_NUMBER",
            "TWILIO_TEST_TO_NUMBER",
        ]
    )


skip_without_twilio = pytest.mark.skipif(
    not _twilio_credentials_available(),
    reason="Twilio credentials not available (set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
    "TWILIO_PHONE_NUMBER, TWILIO_TEST_TO_NUMBER)",
)


@pytest.fixture()
def twilio_settings() -> Settings:
    """Build Settings from real env vars for e2e tests."""
    return Settings(
        twilio_account_sid=os.environ["TWILIO_ACCOUNT_SID"],
        twilio_auth_token=os.environ["TWILIO_AUTH_TOKEN"],
        twilio_phone_number=os.environ["TWILIO_PHONE_NUMBER"],
        twilio_validate_signatures=True,
    )


@pytest.fixture()
def twilio_service(twilio_settings: Settings) -> TwilioService:
    """Real TwilioService wired to actual Twilio API."""
    return TwilioService(svc_settings=twilio_settings)


@pytest.fixture()
def test_to_number() -> str:
    """The verified phone number Twilio trial can send to."""
    return os.environ["TWILIO_TEST_TO_NUMBER"]
