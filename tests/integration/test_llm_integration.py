"""Integration tests that exercise the real amessages() call path.

These tests require ANTHROPIC_API_KEY set in the environment.
They are skipped by default and only run via ``pytest -m integration``.

Run locally:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import UserData
from backend.app.agent.messages import AssistantMessage, UserMessage

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_returns_nonempty_reply(
    integration_user: UserData,
) -> None:
    """ClawboltAgent.process_message() should return a non-empty reply from a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(user=integration_user)
        response = await agent.process_message(
            "Hello, can you help me with a deck estimate?",
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response is not None


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_message_format_accepted(
    integration_user: UserData,
) -> None:
    """The full system prompt + conversation history format should be accepted by a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = ClawboltAgent(user=integration_user)
        history = [
            UserMessage(content="Hi there"),
            AssistantMessage(content="Hello! How can I help?"),
        ]
        response = await agent.process_message(
            "What's a fair price for a 10x10 deck?",
            conversation_history=history,
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response is not None


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_amessages_direct_call() -> None:
    """Verify amessages() works directly with anthropic provider."""
    from any_llm import amessages
    from any_llm.types.messages import MessageResponse

    raw = await amessages(
        model=_ANTHROPIC_MODEL,
        provider="anthropic",
        system="Reply with exactly: HELLO",
        messages=[
            {"role": "user", "content": "Say hello"},
        ],
        max_tokens=50,
    )
    assert isinstance(raw, MessageResponse)

    assert raw.content
    text_parts = [block.text for block in raw.content if block.type == "text"]
    assert text_parts
    assert text_parts[0]
