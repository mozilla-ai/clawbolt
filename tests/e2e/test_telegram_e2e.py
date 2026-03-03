"""End-to-end tests against real Telegram Bot API.

Run with:
    uv run pytest -m e2e -v

Skip with:
    uv run pytest -m "not e2e"
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.telegram_service import TelegramMessagingService
from tests.mocks.llm import make_text_response
from tests.mocks.telegram import make_telegram_update_payload

from .conftest import skip_without_telegram

pytestmark = [pytest.mark.e2e, skip_without_telegram]


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
    telegram_service: TelegramMessagingService,
) -> Generator[TestClient]:
    """FastAPI TestClient wired to real Telegram but in-memory DB."""

    def _override_get_db() -> Generator[Session]:
        yield e2e_db_session

    def _override_get_messaging_service() -> Generator[MessagingService]:
        yield telegram_service

    # Bind background-task SessionLocal to the same in-memory DB so
    # _process_message_background can find the tables and rows created
    # by the request-scoped session.
    test_session_factory = sessionmaker(bind=e2e_db_session.get_bind())

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_messaging_service] = _override_get_messaging_service
    with (
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.ingestion.SessionLocal", test_session_factory),
        # Disable webhook secret validation so e2e tests don't need to derive
        # and send the secret header (the e2e focus is Telegram round-trip).
        patch("backend.app.routers.telegram_webhook.settings.telegram_bot_token", ""),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


# -- Service-level tests -------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_text_message(
    telegram_service: TelegramMessagingService,
    test_chat_id: str,
) -> None:
    """Send a real text message via Telegram and verify message_id returned."""
    if not test_chat_id:
        pytest.skip("TELEGRAM_TEST_CHAT_ID not set")
    msg_id = await telegram_service.send_text(
        to=test_chat_id,
        body="[backshop e2e] text delivery test",
    )
    assert msg_id.isdigit(), f"Expected numeric message_id, got: {msg_id}"


# -- Round-trip test -----------------------------------------------------------


def test_webhook_to_reply_round_trip(
    e2e_client: TestClient,
    e2e_db_session: Session,
    test_chat_id: str,
) -> None:
    """Full round-trip: simulate inbound webhook -> agent replies -> real message sent.

    LLM is mocked for determinism. Telegram is real.
    """
    if not test_chat_id:
        pytest.skip("TELEGRAM_TEST_CHAT_ID not set")

    expected_reply = "[backshop e2e] I can help with that deck estimate!"

    with patch(
        "backend.app.agent.core.acompletion",
        new_callable=AsyncMock,
        return_value=make_text_response(expected_reply),
    ):
        payload = make_telegram_update_payload(
            chat_id=int(test_chat_id),
            text="I need a quote for a 12x12 composite deck",
        )
        response = e2e_client.post("/api/webhooks/telegram", json=payload)

    assert response.status_code == 200

    # Verify backshop stored both inbound and outbound messages
    from backend.app.models import Message

    messages = e2e_db_session.query(Message).order_by(Message.id).all()
    assert len(messages) == 2, f"Expected 2 messages (in+out), got {len(messages)}"
    assert messages[0].direction == "inbound"
    assert messages[1].direction == "outbound"
    assert messages[1].body == expected_reply
