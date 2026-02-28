from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.models import Contractor
from backend.app.services.twilio_service import TwilioService, get_twilio_service


@pytest.fixture()
def db_session() -> Generator[Session]:
    """Fresh in-memory SQLite per test."""
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
def test_contractor(db_session: Session) -> Contractor:
    """Create a test contractor."""
    contractor = Contractor(
        user_id="test-user-001",
        name="Test Contractor",
        phone="+15551234567",
        trade="General Contractor",
        location="Portland, OR",
    )
    db_session.add(contractor)
    db_session.commit()
    db_session.refresh(contractor)
    return contractor


@pytest.fixture()
def mock_twilio_service() -> TwilioService:
    """Mock TwilioService that doesn't hit real Twilio."""
    service = MagicMock(spec=TwilioService)
    service.send_sms = AsyncMock(return_value="SM_mock_sid")
    service.send_mms = AsyncMock(return_value="SM_mock_sid")
    service.send_message = AsyncMock(return_value="SM_mock_sid")
    return service


@pytest.fixture()
def client(
    db_session: Session, test_contractor: Contractor, mock_twilio_service: TwilioService
) -> Generator[TestClient]:
    """FastAPI test client with overridden DB, auth, and Twilio."""

    def _override_get_db() -> Generator[Session]:
        yield db_session

    def _override_get_twilio_service() -> Generator[TwilioService]:
        yield mock_twilio_service

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_twilio_service] = _override_get_twilio_service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
