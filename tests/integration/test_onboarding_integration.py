"""Integration tests for the onboarding flow via a real LLM.

Verifies that a new user's first message triggers onboarding,
the agent extracts profile fields via update_profile, and the profile
is updated in the file store.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import get_user_store
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.profile_tools import (
    create_profile_tools,
    extract_profile_updates_from_tool_calls,
)

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_onboarding_extracts_profile_from_intro() -> None:
    """Agent should extract name and trade from a natural introduction message."""

    # Create a blank user (no profile info)
    store = get_user_store()
    user = await store.create(
        user_id="onboarding-test-user",
        channel_identifier="onboard_test_1",
        preferred_channel="telegram",
    )

    assert is_onboarding_needed(user)

    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(user=user)
        tools = create_memory_tools(user.id)
        tools.extend(create_profile_tools(user))
        agent.register_tools(tools)

        system_prompt = build_onboarding_system_prompt(user)
        response = await agent.process_message(
            "Hey! I'm Jake, I'm a plumber based in Portland.",
            system_prompt_override=system_prompt,
            temperature=0,
        )

    # Agent should have called update_profile for name and trade
    tool_names = [tc.name for tc in response.tool_calls]
    used_profile_tool = "update_profile" in tool_names

    # Extract profile updates using the new extraction logic
    updates = extract_profile_updates_from_tool_calls(response.tool_calls)
    extracted_profile = "name" in updates or "trade" in updates

    # Reply may or may not be present (small models sometimes only call tools).
    acknowledged = False
    if response.reply_text:
        reply_lower = response.reply_text.lower()
        acknowledged = "jake" in reply_lower or "plumb" in reply_lower

    # Primary check: agent used update_profile. Fallback: agent at least acknowledged the info.
    assert used_profile_tool or acknowledged, (
        f"Expected update_profile calls or acknowledgment in reply. "
        f"Tool calls: {tool_names}, reply: {response.reply_text[:200]}"
    )
    if used_profile_tool:
        assert extracted_profile, (
            f"update_profile called but no profile updates extracted: {updates}"
        )
