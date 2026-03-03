"""Tests for LLM usage tracking."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent.core import BackshopAgent
from backend.app.database import Base
from backend.app.models import Contractor, LLMUsageLog
from backend.app.services.llm_usage import log_llm_usage
from tests.mocks.llm import make_text_response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Generator[Session]:
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
def contractor(db: Session) -> Contractor:
    c = Contractor(
        user_id="usage-test-001",
        name="Usage Tester",
        phone="+15550001111",
        trade="Plumber",
        location="Portland, OR",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def _make_response_with_usage(
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    total_tokens: int = 150,
) -> MagicMock:
    """Build a mock LLM response with usage data."""
    resp = make_text_response("Hello!")
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    resp.usage = usage
    return resp


def test_log_llm_usage_saves_to_db(db: Session, contractor: Contractor) -> None:
    """log_llm_usage should persist token counts to the database."""
    response = _make_response_with_usage(prompt_tokens=200, completion_tokens=80, total_tokens=280)

    entry = log_llm_usage(db, contractor.id, "gpt-4o", response, "agent_main")

    assert entry is not None
    assert entry.contractor_id == contractor.id
    assert entry.model == "gpt-4o"
    assert entry.prompt_tokens == 200
    assert entry.completion_tokens == 80
    assert entry.total_tokens == 280
    assert entry.purpose == "agent_main"

    # Verify it's actually in the DB
    rows = db.query(LLMUsageLog).filter(LLMUsageLog.contractor_id == contractor.id).all()
    assert len(rows) == 1


def test_log_llm_usage_no_usage_data(db: Session, contractor: Contractor) -> None:
    """log_llm_usage should return None when response has no usage data."""
    response = make_text_response("Hello!")
    # MagicMock attributes auto-create, so explicitly set usage to None
    response.usage = None

    entry = log_llm_usage(db, contractor.id, "gpt-4o", response, "agent_main")

    assert entry is None
    rows = db.query(LLMUsageLog).filter(LLMUsageLog.contractor_id == contractor.id).all()
    assert len(rows) == 0


def test_log_llm_usage_zero_tokens(db: Session, contractor: Contractor) -> None:
    """log_llm_usage should handle zero token counts gracefully."""
    response = _make_response_with_usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    entry = log_llm_usage(db, contractor.id, "gpt-4o", response, "heartbeat")

    assert entry is not None
    assert entry.prompt_tokens == 0
    assert entry.completion_tokens == 0
    assert entry.total_tokens == 0


def test_log_llm_usage_computes_total_when_missing(db: Session, contractor: Contractor) -> None:
    """log_llm_usage should compute total_tokens when it is 0 or None."""
    response = _make_response_with_usage(prompt_tokens=100, completion_tokens=50, total_tokens=0)

    entry = log_llm_usage(db, contractor.id, "gpt-4o", response, "agent_main")

    assert entry is not None
    # total_tokens should be computed as prompt + completion
    assert entry.total_tokens == 150


def test_log_llm_usage_multiple_entries(db: Session, contractor: Contractor) -> None:
    """Multiple log_llm_usage calls should create separate rows."""
    for i in range(3):
        response = _make_response_with_usage(
            prompt_tokens=100 * (i + 1),
            completion_tokens=50 * (i + 1),
            total_tokens=150 * (i + 1),
        )
        log_llm_usage(db, contractor.id, "gpt-4o", response, f"purpose_{i}")

    rows = db.query(LLMUsageLog).filter(LLMUsageLog.contractor_id == contractor.id).all()
    assert len(rows) == 3
    assert rows[0].purpose == "purpose_0"
    assert rows[1].purpose == "purpose_1"
    assert rows[2].purpose == "purpose_2"


def test_log_llm_usage_different_models(db: Session, contractor: Contractor) -> None:
    """log_llm_usage should correctly record different model names."""
    for model_name in ["gpt-4o", "gpt-4o-mini", "claude-3-haiku"]:
        response = _make_response_with_usage()
        log_llm_usage(db, contractor.id, model_name, response, "agent_main")

    rows = db.query(LLMUsageLog).filter(LLMUsageLog.contractor_id == contractor.id).all()
    models = {r.model for r in rows}
    assert models == {"gpt-4o", "gpt-4o-mini", "claude-3-haiku"}


# ---------------------------------------------------------------------------
# Integration: agent process_message logs usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_process_message_logs_usage(
    mock_acompletion: MagicMock,
    db: Session,
    contractor: Contractor,
) -> None:
    """BackshopAgent.process_message should call log_llm_usage after acompletion."""
    response = _make_response_with_usage(prompt_tokens=300, completion_tokens=120, total_tokens=420)
    mock_acompletion.return_value = response

    agent = BackshopAgent(db=db, contractor=contractor)
    await agent.process_message("What is my schedule today?")

    rows = db.query(LLMUsageLog).filter(LLMUsageLog.contractor_id == contractor.id).all()
    assert len(rows) == 1
    assert rows[0].purpose == "agent_main"
    assert rows[0].total_tokens == 420
