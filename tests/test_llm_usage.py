"""Tests for LLM usage tracking."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from any_llm.types.messages import MessageResponse, MessageUsage

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import ContractorData, LLMUsageStore, _read_jsonl
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
    """Build a MessageResponse with custom usage data.

    The parameter names use prompt_tokens/completion_tokens to match the
    storage column names; they map to input_tokens/output_tokens in the
    Messages API response format. The total_tokens parameter is kept for
    call-site clarity but is not used (total is always computed).
    """
    resp = make_text_response("Hello!")
    resp.usage = MessageUsage(input_tokens=prompt_tokens, output_tokens=completion_tokens)
    return resp


def _read_usage_entries(contractor_id: int) -> list[dict[str, object]]:
    """Read all LLM usage entries for a contractor."""
    store = LLMUsageStore(contractor_id)
    return _read_jsonl(store._path)


def test_log_llm_usage_saves(test_contractor: ContractorData) -> None:
    """log_llm_usage should persist token counts to the usage log."""
    response = _make_response_with_usage(prompt_tokens=200, completion_tokens=80, total_tokens=280)

    log_llm_usage(test_contractor.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_contractor.id)
    assert len(entries) == 1
    assert entries[0]["contractor_id"] == test_contractor.id
    assert entries[0]["model"] == "test-model"
    assert entries[0]["prompt_tokens"] == 200
    assert entries[0]["completion_tokens"] == 80
    assert entries[0]["total_tokens"] == 280
    assert entries[0]["purpose"] == "agent_main"


def test_log_llm_usage_zero_tokens(test_contractor: ContractorData) -> None:
    """log_llm_usage should handle zero token counts gracefully."""
    response = _make_response_with_usage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    log_llm_usage(test_contractor.id, "test-model", response, "heartbeat")

    entries = _read_usage_entries(test_contractor.id)
    assert len(entries) == 1
    assert entries[0]["prompt_tokens"] == 0
    assert entries[0]["completion_tokens"] == 0
    assert entries[0]["total_tokens"] == 0


def test_log_llm_usage_computes_total(test_contractor: ContractorData) -> None:
    """log_llm_usage should compute total_tokens as prompt + completion."""
    response = _make_response_with_usage(prompt_tokens=100, completion_tokens=50, total_tokens=0)

    log_llm_usage(test_contractor.id, "test-model", response, "agent_main")

    entries = _read_usage_entries(test_contractor.id)
    assert len(entries) == 1
    # total_tokens should be computed as prompt + completion
    assert entries[0]["total_tokens"] == 150


def test_log_llm_usage_multiple_entries(test_contractor: ContractorData) -> None:
    """Multiple log_llm_usage calls should create separate entries."""
    for i in range(3):
        response = _make_response_with_usage(
            prompt_tokens=100 * (i + 1),
            completion_tokens=50 * (i + 1),
            total_tokens=150 * (i + 1),
        )
        log_llm_usage(test_contractor.id, "test-model", response, f"purpose_{i}")

    entries = _read_usage_entries(test_contractor.id)
    assert len(entries) == 3
    assert entries[0]["purpose"] == "purpose_0"
    assert entries[1]["purpose"] == "purpose_1"
    assert entries[2]["purpose"] == "purpose_2"


def test_log_llm_usage_different_models(test_contractor: ContractorData) -> None:
    """log_llm_usage should correctly record different model names."""
    for model_name in ["model-a", "model-b", "model-c"]:
        response = _make_response_with_usage()
        log_llm_usage(test_contractor.id, model_name, response, "agent_main")

    entries = _read_usage_entries(test_contractor.id)
    models = {r["model"] for r in entries}
    assert models == {"model-a", "model-b", "model-c"}


# ---------------------------------------------------------------------------
# Integration: agent process_message logs usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_process_message_logs_usage(
    mock_amessages: MagicMock,
    test_contractor: ContractorData,
) -> None:
    """ClawboltAgent.process_message should call log_llm_usage after acompletion."""
    response = _make_response_with_usage(prompt_tokens=300, completion_tokens=120, total_tokens=420)
    mock_amessages.return_value = response

    agent = ClawboltAgent(contractor=test_contractor)
    await agent.process_message("What is my schedule today?")

    entries = _read_usage_entries(test_contractor.id)
    assert len(entries) == 1
    assert entries[0]["purpose"] == "agent_main"
    assert entries[0]["total_tokens"] == 420
