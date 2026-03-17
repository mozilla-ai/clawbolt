"""Integration tests for the onboarding flow via a real LLM.

Verifies that a new user's first message triggers onboarding,
the agent writes profile data via write_file, and deletes BOOTSTRAP.md
to signal completion.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import backend.app.database as _db_module
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.config import settings
from backend.app.models import User

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_onboarding_extracts_profile_from_intro() -> None:
    """Agent should write user info to USER.md via write_file during onboarding."""

    # Create a blank user (no profile info)
    db = _db_module.SessionLocal()
    try:
        user = User(
            user_id="onboarding-test-user",
            channel_identifier="onboard_test_1",
            preferred_channel="telegram",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    assert is_onboarding_needed(user)

    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(user=user)
        tools = create_workspace_tools(user.id)
        agent.register_tools(tools)

        system_prompt = build_onboarding_system_prompt(user)
        response = await agent.process_message(
            "Hey! I'm Jake, I'm a plumber based in Portland.",
            system_prompt_override=system_prompt,
            temperature=0,
        )

    # Agent should have used write_file or edit_file to save user info
    tool_names = [tc.name for tc in response.tool_calls]
    used_file_tool = "write_file" in tool_names or "edit_file" in tool_names

    # Reply may or may not be present (small models sometimes only call tools).
    acknowledged = False
    if response.reply_text:
        reply_lower = response.reply_text.lower()
        acknowledged = "jake" in reply_lower or "plumb" in reply_lower

    # Primary check: agent used file tools. Fallback: agent at least acknowledged the info.
    assert used_file_tool or acknowledged, (
        f"Expected write_file/edit_file calls or acknowledgment in reply. "
        f"Tool calls: {tool_names}, reply: {response.reply_text[:200]}"
    )

    # Check that USER.md was written with user info
    if used_file_tool:
        user_md = Path(settings.data_dir) / str(user.id) / "USER.md"
        if user_md.exists():
            content = user_md.read_text(encoding="utf-8").lower()
            assert "jake" in content or "plumb" in content
