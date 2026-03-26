"""Tests for LLM usage tracking."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from any_llm.types.messages import MessageResponse, MessageUsage

import backend.app.database as _db_module
from backend.app.agent.core import ClawboltAgent
from backend.app.models import LLMUsageLog, User
from backend.app.services.llm_usage import log_llm_usage
from tests.mocks.llm import make_text_response

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def _make_response_with_usage(
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    total_tokens: int = 150,
) -> MessageResponse:
    """Build a MessageResponse with custom usage data."""
    resp = make_text_response("Hello!")
    resp.usage = MessageUsage(input_tokens=prompt_tokens, output_tokens=completion_tokens)
    return resp


def _read_usage_entries(user_id: str) -> list[dict[str, object]]:
    """Read all LLM usage entries for a user from the database."""
    db = _db_module.SessionLocal()
    try:
        logs = (
            db.query(LLMUsageLog).filter_by(user_id=user_id).order_by(LLMUsageLog.created_at).all()
        )
        return [
            {
                "user_id": log.user_id,
                "model": log.model,
                "prompt_tokens": log.input_tokens,
                "completion_tokens": log.output_tokens,
                "total_tokens": log.total_tokens,
                "purpose": log.purpose,
                "cache_creation_input_tokens": log.cache_creation_input_tokens,
                "cache_read_input_tokens": log.cache_read_input_tokens,
            }
            for log in logs
        ]
    finally:
        db.close()


def test_log_llm_usage_saves(test_user: User) -> None:
    """log_llm_usage should persist token counts to the usage log."""
    response = _make_response_with_usage(prompt_tokens=200, completion_tokens=80, total_tokens=280)

    log_llm_usage(test_user.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["user_id"] == test_user.id
    assert entries[0]["model"] == "test-model"
    assert entries[0]["prompt_tokens"] == 200
    assert entries[0]["completion_tokens"] == 80
    assert entries[0]["total_tokens"] == 280
    assert entries[0]["purpose"] == "agent_main"


def test_log_llm_usage_zero_tokens(test_user: User) -> None:
    """log_llm_usage should handle zero token counts gracefully."""
    response = _make_response_with_usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    log_llm_usage(test_user.id, "test-model", response, "heartbeat")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["prompt_tokens"] == 0
    assert entries[0]["completion_tokens"] == 0
    assert entries[0]["total_tokens"] == 0


def test_log_llm_usage_computes_total(test_user: User) -> None:
    """log_llm_usage should compute total_tokens as prompt + completion."""
    response = _make_response_with_usage(prompt_tokens=100, completion_tokens=50, total_tokens=0)

    log_llm_usage(test_user.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["total_tokens"] == 150


def test_log_llm_usage_multiple_entries(test_user: User) -> None:
    """Multiple log_llm_usage calls should create separate entries."""
    for i in range(3):
        response = _make_response_with_usage(
            prompt_tokens=100 * (i + 1),
            completion_tokens=50 * (i + 1),
            total_tokens=150 * (i + 1),
        )
        log_llm_usage(test_user.id, "test-model", response, f"purpose_{i}")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 3
    assert entries[0]["purpose"] == "purpose_0"
    assert entries[1]["purpose"] == "purpose_1"
    assert entries[2]["purpose"] == "purpose_2"


def test_log_llm_usage_different_models(test_user: User) -> None:
    """log_llm_usage should correctly record different model names."""
    for model_name in ["model-a", "model-b", "model-c"]:
        response = _make_response_with_usage()
        log_llm_usage(test_user.id, model_name, response, "agent_main")

    entries = _read_usage_entries(test_user.id)
    models = {r["model"] for r in entries}
    assert models == {"model-a", "model-b", "model-c"}


# ---------------------------------------------------------------------------
# Integration: agent process_message logs usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_process_message_logs_usage(
    mock_amessages: MagicMock,
    test_user: User,
) -> None:
    """ClawboltAgent.process_message should call log_llm_usage after acompletion."""
    response = _make_response_with_usage(prompt_tokens=300, completion_tokens=120, total_tokens=420)
    mock_amessages.return_value = response

    agent = ClawboltAgent(user=test_user)
    await agent.process_message("What is my schedule today?")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["purpose"] == "agent_main"
    assert entries[0]["total_tokens"] == 420


# ---------------------------------------------------------------------------
# Cache token tracking tests
# ---------------------------------------------------------------------------


def test_log_llm_usage_cache_tokens_stored(test_user: User) -> None:
    """log_llm_usage should persist cache token fields when present."""
    response = _make_response_with_usage(prompt_tokens=500, completion_tokens=100)
    response.usage.cache_creation_input_tokens = 200
    response.usage.cache_read_input_tokens = 300

    log_llm_usage(test_user.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["cache_creation_input_tokens"] == 200
    assert entries[0]["cache_read_input_tokens"] == 300


def test_log_llm_usage_cache_tokens_null_when_absent(test_user: User) -> None:
    """Cache token fields should be NULL when not set on the response."""
    response = _make_response_with_usage(prompt_tokens=100, completion_tokens=50)

    log_llm_usage(test_user.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_user.id)
    assert len(entries) == 1
    assert entries[0]["cache_creation_input_tokens"] is None
    assert entries[0]["cache_read_input_tokens"] is None
