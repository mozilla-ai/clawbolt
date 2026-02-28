"""Shared fixtures for integration tests that hit a local LLM server."""

import os

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import Contractor

_LMSTUDIO_URL = "http://localhost:1234/v1"


def _lmstudio_available() -> bool:
    """Check if LM Studio is reachable."""
    try:
        resp = httpx.get(f"{_LMSTUDIO_URL}/models", timeout=2)
        return resp.status_code == 200
    except httpx.ConnectError:
        return False


skip_without_lmstudio = pytest.mark.skipif(
    not _lmstudio_available(),
    reason="LM Studio not available at localhost:1234",
)


@pytest.fixture()
def integration_db() -> Session:
    """Fresh in-memory SQLite for integration tests."""
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
def integration_contractor(integration_db: Session) -> Contractor:
    """Test contractor for integration tests."""
    contractor = Contractor(
        user_id="integration-test-user",
        name="Integration Test Contractor",
        phone="+15559999999",
        trade="General Contractor",
        location="Portland, OR",
    )
    integration_db.add(contractor)
    integration_db.commit()
    integration_db.refresh(contractor)
    return contractor


@pytest.fixture()
def lmstudio_model() -> str:
    """The model loaded in LM Studio (override via LLM_MODEL env var)."""
    return os.environ.get("LLM_MODEL", "google/gemma-3-4b")
