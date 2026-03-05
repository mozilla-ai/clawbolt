"""Integration tests for memory save and recall via a real LLM.

Verifies that the agent calls save_fact when told information and
recall_facts when asked to remember it.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.models import Contractor, Memory

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_memory_save_via_llm(
    integration_db: Session,
    integration_contractor: Contractor,
) -> None:
    """Agent should call save_fact when told new information."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(db=integration_db, contractor=integration_contractor)
        tools = create_memory_tools(integration_db, integration_contractor.id)
        agent.register_tools(tools)

        response = await agent.process_message(
            "Remember this: my hourly rate is $85 and I specialize in kitchen remodels.",
        )

    # Agent should have saved at least one fact
    tool_names = [tc.name for tc in response.tool_calls]
    assert "save_fact" in tool_names, f"Expected save_fact call, got: {tool_names}"

    # Memory records should exist in DB
    memories = (
        integration_db.query(Memory).filter(Memory.contractor_id == integration_contractor.id).all()
    )
    assert len(memories) >= 1

    # At least one memory should contain rate or kitchen info
    all_values = " ".join(m.value.lower() for m in memories)
    assert "85" in all_values or "kitchen" in all_values


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_memory_save_then_recall(
    integration_db: Session,
    integration_contractor: Contractor,
) -> None:
    """Agent should recall previously saved facts when asked."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(db=integration_db, contractor=integration_contractor)
        tools = create_memory_tools(integration_db, integration_contractor.id)
        agent.register_tools(tools)

        # Step 1: Save a fact
        await agent.process_message(
            "Remember that my hourly rate is $85.",
        )

        # Verify it was saved
        memories = (
            integration_db.query(Memory)
            .filter(Memory.contractor_id == integration_contractor.id)
            .all()
        )
        assert len(memories) >= 1

        # Step 2: Ask about it in a new agent call
        agent2 = ClawboltAgent(db=integration_db, contractor=integration_contractor)
        tools2 = create_memory_tools(integration_db, integration_contractor.id)
        agent2.register_tools(tools2)

        response = await agent2.process_message(
            "What's my hourly rate? Check your memory.",
        )

    # The saved fact should be in the DB (deterministic check)
    all_values = " ".join(m.value for m in memories)
    assert "85" in all_values

    # The reply should mention $85 (LLM gets it via system prompt memory context)
    # The LLM may phrase it differently, so also accept tool-based recall
    recalled = any(tc.name == "recall_facts" for tc in response.tool_calls)
    assert "85" in response.reply_text or recalled, (
        f"Expected '85' in reply or recall_facts call. Reply: {response.reply_text}"
    )
