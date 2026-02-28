"""Shared fixtures for integration tests that hit a real LLM API."""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import Contractor

_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

skip_without_anthropic_key = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
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
def onboarded_contractor(integration_db: Session) -> Contractor:
    """Onboarded contractor with business hours for heartbeat tests."""
    contractor = Contractor(
        user_id="heartbeat-integration-user",
        name="Mike the Plumber",
        phone="+15559990000",
        trade="Plumber",
        location="Portland, OR",
        business_hours="7am-5pm",
        onboarding_complete=True,
    )
    integration_db.add(contractor)
    integration_db.commit()
    integration_db.refresh(contractor)
    return contractor
