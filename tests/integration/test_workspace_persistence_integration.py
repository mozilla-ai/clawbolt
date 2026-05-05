"""Integration tests for mid-conversation persistence to SOUL.md and MEMORY.md.

Regression coverage for #1133: when the user gives behavioral feedback
("treat 'looks good' as confirmation") or states a durable business fact
("Acme Plumbing's contact is jane@acme.example"), the agent must call
edit_file or write_file in the same turn rather than only acknowledging
verbally. Verbal-only acknowledgement is the bug.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.system_prompt import build_agent_system_prompt
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.models import User

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key

_PERSISTENCE_TOOLS = {"write_file", "edit_file"}


def _persistence_call_for_path(response: object, path_token: str) -> tuple[bool, list[str]]:
    """Return (matched, tool_calls_seen) for a write/edit on *path_token*.

    The match is loose: any write_file or edit_file whose serialized args
    mention the file name. Different models structure tool args slightly
    differently, so we match on substring rather than exact path equality.
    """
    seen: list[str] = []
    matched = False
    for call in response.tool_calls:  # type: ignore[attr-defined]
        seen.append(call.name)
        if call.name not in _PERSISTENCE_TOOLS:
            continue
        if path_token in str(call.args):
            matched = True
    return matched, seen


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_behavioral_feedback_persists_to_soul_md(
    onboarded_user: User,
) -> None:
    """Behavioral feedback must trigger an edit_file/write_file on SOUL.md (#1133)."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 800
        mock_settings.context_trim_target_tokens = 400_000

        agent = ClawboltAgent(user=onboarded_user)
        tools = create_workspace_tools(onboarded_user.id)
        agent.register_tools(tools)

        system_prompt = await build_agent_system_prompt(
            user=onboarded_user,
            tools=tools,
            message_context="",
        )
        response = await agent.process_message(
            "From now on, treat 'looks good' from me as confirmation. "
            "Stop asking me to confirm before saving files.",
            system_prompt_override=system_prompt,
            temperature=0,
        )

    matched, seen = _persistence_call_for_path(response, "SOUL.md")
    assert matched, (
        "Expected edit_file or write_file on SOUL.md after behavioral feedback. "
        f"Tool calls seen: {seen}. Reply: {response.reply_text[:200]!r}"
    )


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_durable_business_fact_persists_to_memory_md(
    onboarded_user: User,
) -> None:
    """Durable business facts must trigger an edit_file/write_file on MEMORY.md (#1133)."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 800
        mock_settings.context_trim_target_tokens = 400_000

        agent = ClawboltAgent(user=onboarded_user)
        tools = create_workspace_tools(onboarded_user.id)
        agent.register_tools(tools)

        system_prompt = await build_agent_system_prompt(
            user=onboarded_user,
            tools=tools,
            message_context="",
        )
        response = await agent.process_message(
            "Quick fact for the file: Acme Plumbing's billing contact is "
            "jane@acme.example, and we charge them $95/hr for emergency calls.",
            system_prompt_override=system_prompt,
            temperature=0,
        )

    matched, seen = _persistence_call_for_path(response, "MEMORY.md")
    assert matched, (
        "Expected edit_file or write_file on MEMORY.md after a durable business fact. "
        f"Tool calls seen: {seen}. Reply: {response.reply_text[:200]!r}"
    )
