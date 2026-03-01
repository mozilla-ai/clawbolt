"""Integration tests for estimate generation via a real LLM.

Verifies that the agent calls generate_estimate when asked for a quote
and that the full pipeline (tool call -> DB records -> PDF) works.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.tools.estimate_tools import create_estimate_tools
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.models import Contractor, Estimate, EstimateLineItem

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_estimate_generation_roundtrip(
    integration_db: Session,
    integration_contractor: Contractor,
) -> None:
    """Agent should call generate_estimate tool and create DB records when asked for a quote."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = BackshopAgent(db=integration_db, contractor=integration_contractor)
        tools = create_estimate_tools(integration_db, integration_contractor)
        tools.extend(create_memory_tools(integration_db, integration_contractor.id))
        agent.register_tools(tools)

        response = await agent.process_message(
            "I need an estimate for a deck build. "
            "Labor: 20 hours at $75/hr. Materials: $1,200 for composite decking.",
        )

    # Agent should have called the estimate tool
    tool_names = [tc["name"] for tc in response.tool_calls]
    assert "generate_estimate" in tool_names, f"Expected generate_estimate call, got: {tool_names}"

    # Estimate record should exist in DB
    estimates = integration_db.query(Estimate).all()
    assert len(estimates) == 1
    assert estimates[0].status == "draft"
    assert estimates[0].total_amount > 0

    # Line items should exist
    line_items = integration_db.query(EstimateLineItem).all()
    assert len(line_items) >= 1

    # Reply should mention the estimate
    assert response.reply_text
