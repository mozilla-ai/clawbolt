"""End-to-end tests against real Twilio API.

Tests fall into two categories:
  - Service-level: test TwilioService directly (send SMS, validate signatures)
  - Round-trip: POST to webhook -> agent processes -> real SMS sent -> verify via API

LLM calls are mocked to keep tests deterministic and free. Only Twilio is real.

Run with:
    uv run pytest -m e2e -v

Skip with:
    uv run pytest -m "not e2e"
"""

import time
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from backend.app.config import Settings
from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.services.twilio_service import TwilioService, get_twilio_service
from tests.mocks.llm import make_text_response
from tests.mocks.twilio import make_twilio_webhook_payload

from .conftest import skip_without_twilio

pytestmark = [pytest.mark.e2e, skip_without_twilio]


# -- Helpers -------------------------------------------------------------------


def _fetch_message(twilio_settings: Settings, sid: str) -> object:
    """Fetch a message resource from Twilio by SID."""
    client = TwilioClient(twilio_settings.twilio_account_sid, twilio_settings.twilio_auth_token)
    return client.messages(sid).fetch()


def _find_recent_outbound(
    twilio_settings: Settings,
    from_number: str,
    to_number: str,
    after_sid: str | None = None,
    timeout_seconds: int = 15,
) -> object | None:
    """Poll Twilio for a recent outbound message matching from/to."""
    client = TwilioClient(twilio_settings.twilio_account_sid, twilio_settings.twilio_auth_token)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        messages = client.messages.list(from_=from_number, to=to_number, limit=5)
        for msg in messages:
            # Skip messages sent before our test (if we have a reference SID)
            if after_sid and msg.sid == after_sid:
                break
            return msg
        time.sleep(2)
    return None


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture()
def e2e_db_session() -> Generator[Session]:
    """Fresh in-memory SQLite for e2e tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def e2e_client(
    e2e_db_session: Session,
    twilio_service: TwilioService,
) -> Generator[TestClient]:
    """FastAPI TestClient wired to real Twilio but in-memory DB."""

    def _override_get_db() -> Generator[Session]:
        yield e2e_db_session

    def _override_get_twilio_service() -> Generator[TwilioService]:
        yield twilio_service

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_twilio_service] = _override_get_twilio_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# -- Service-level tests -------------------------------------------------------


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


# -- Round-trip test -----------------------------------------------------------


def test_webhook_to_sms_round_trip(
    e2e_client: TestClient,
    e2e_db_session: Session,
    twilio_settings: Settings,
    test_to_number: str,
) -> None:
    """Full round-trip: simulate inbound webhook -> agent replies -> real SMS sent.

    LLM is mocked for determinism. Twilio is real.
    """
    expected_reply = "[backshop e2e] I can help with that deck estimate!"

    with patch(
        "backend.app.agent.core.acompletion",
        new_callable=AsyncMock,
        return_value=make_text_response(expected_reply),
    ):
        payload = make_twilio_webhook_payload(
            from_number=test_to_number,
            body="I need a quote for a 12x12 composite deck",
            to_number=twilio_settings.twilio_phone_number,
        )
        response = e2e_client.post(
            "/api/webhooks/twilio/inbound",
            data=payload,
        )

    assert response.status_code == 200

    # Verify backshop stored both inbound and outbound messages
    from backend.app.models import Message

    messages = e2e_db_session.query(Message).order_by(Message.id).all()
    assert len(messages) == 2, f"Expected 2 messages (in+out), got {len(messages)}"
    assert messages[0].direction == "inbound"
    assert messages[1].direction == "outbound"
    assert messages[1].body == expected_reply

    # Verify the real SMS was sent by checking the Twilio API
    outbound_msg = _find_recent_outbound(
        twilio_settings,
        from_number=twilio_settings.twilio_phone_number,
        to_number=test_to_number,
        timeout_seconds=15,
    )
    assert outbound_msg is not None, "No outbound SMS found in Twilio message logs"
    assert expected_reply in outbound_msg.body
