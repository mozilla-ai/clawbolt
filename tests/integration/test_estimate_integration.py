"""Integration tests for estimate generation via a real LLM.

Verifies that the agent calls generate_estimate when asked for a quote
and that the full pipeline (tool call -> file store records -> PDF) works.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import EstimateStore, UserData
from backend.app.agent.tools.estimate_tools import create_estimate_tools
from backend.app.agent.tools.memory_tools import create_memory_tools

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_estimate_generation_roundtrip(
    integration_user: UserData,
) -> None:
    """Agent should call generate_estimate tool and create file store records when asked."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(user=integration_user)
        tools = create_estimate_tools(integration_user)
        tools.extend(create_memory_tools(integration_user.id))
        agent.register_tools(tools)

        response = await agent.process_message(
            "Use the generate_estimate tool to create an estimate for a deck build. "
            "Labor: 20 hours at $75/hr. Materials: $1,200 for composite decking.",
            system_prompt_override=(
                "You are a user assistant. When the user provides estimate"
                " details, always call the generate_estimate tool with the line"
                " items. Do not respond with text only."
            ),
            temperature=0,
        )

    # Agent should have called the estimate tool
    tool_names = [tc.name for tc in response.tool_calls]
    assert "generate_estimate" in tool_names, f"Expected generate_estimate call, got: {tool_names}"

    # Estimate record should exist in the file store
    store = EstimateStore(integration_user.id)
    estimates = await store.list_all()
    assert len(estimates) == 1
    assert estimates[0].status == "draft"
    assert estimates[0].total_amount > 0

    # Line items should exist
    assert len(estimates[0].line_items) >= 1

    # Reply should mention the estimate
    assert response.reply_text
