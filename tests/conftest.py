from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.models import Contractor


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
def client(db_session: Session, test_contractor: Contractor) -> Generator[TestClient]:
    """FastAPI test client with overridden DB and auth."""

    def _override_get_db() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
