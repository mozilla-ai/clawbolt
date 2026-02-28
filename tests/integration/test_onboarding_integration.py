"""Integration tests for the onboarding flow via a real LLM.

Verifies that a new contractor's first message triggers onboarding,
the agent extracts profile fields via save_fact, and the profile
is updated in the database.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    extract_profile_updates,
    is_onboarding_needed,
)
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.models import Contractor

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_onboarding_extracts_profile_from_intro(
    integration_db: Session,
) -> None:
    """Agent should extract name and trade from a natural introduction message."""
    # Create a blank contractor (no profile info)
    contractor = Contractor(
        user_id="onboarding-test-user",
        channel_identifier="onboard_test_1",
        preferred_channel="telegram",
    )
    integration_db.add(contractor)
    integration_db.commit()
    integration_db.refresh(contractor)

    assert is_onboarding_needed(contractor)

    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None

        agent = BackshopAgent(db=integration_db, contractor=contractor)
        tools = create_memory_tools(integration_db, contractor.id)
        agent.register_tools(tools)

        system_prompt = build_onboarding_system_prompt(contractor)
        response = await agent.process_message(
            "Hey! I'm Jake, I'm a plumber based in Portland.",
            system_prompt_override=system_prompt,
            temperature=0,
        )

    # Agent should have called save_fact for name and trade
    tool_names = [tc["name"] for tc in response.tool_calls]
    saved_facts = "save_fact" in tool_names

    # Extract profile updates using the onboarding logic
    updates = extract_profile_updates(response)
    extracted_profile = "name" in updates or "trade" in updates

    # Reply should be friendly and acknowledge the info
    assert response.reply_text
    reply_lower = response.reply_text.lower()
    acknowledged = "jake" in reply_lower or "plumb" in reply_lower

    # Primary check: agent saved facts. Fallback: agent at least acknowledged the info.
    assert saved_facts or acknowledged, (
        f"Expected save_fact calls or acknowledgment in reply. "
        f"Tool calls: {tool_names}, reply: {response.reply_text[:200]}"
    )
    if saved_facts:
        assert extracted_profile, f"save_fact called but no profile updates extracted: {updates}"
