"""Integration tests that exercise the real acompletion() call path.

These tests require ANTHROPIC_API_KEY set in the environment.
They are skipped by default and only run via ``pytest -m integration``.

Run locally:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.messages import AssistantMessage, UserMessage
from backend.app.models import Contractor

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_returns_nonempty_reply(
    integration_db: Session,
    integration_contractor: Contractor,
) -> None:
    """BackshopAgent.process_message() should return a non-empty reply from a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = BackshopAgent(db=integration_db, contractor=integration_contractor)
        response = await agent.process_message(
            "Hello, can you help me with a deck estimate?",
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response.reply_text
    assert len(response.reply_text) > 0


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_message_format_accepted(
    integration_db: Session,
    integration_contractor: Contractor,
) -> None:
    """The full system prompt + conversation history format should be accepted by a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500

        agent = BackshopAgent(db=integration_db, contractor=integration_contractor)
        history = [
            UserMessage(content="Hi there"),
            AssistantMessage(content="Hello! How can I help?"),
        ]
        response = await agent.process_message(
            "What's a fair price for a 10x10 deck?",
            conversation_history=history,
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response.reply_text
    assert len(response.reply_text) > 0


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_acompletion_direct_call() -> None:
    """Verify acompletion() works directly with anthropic provider."""
    from any_llm import acompletion
    from any_llm.types.completion import ChatCompletion

    raw = await acompletion(
        model=_ANTHROPIC_MODEL,
        provider="anthropic",
        messages=[
            {"role": "system", "content": "Reply with exactly: HELLO"},
            {"role": "user", "content": "Say hello"},
        ],
        max_tokens=50,
    )
    assert isinstance(raw, ChatCompletion)

    assert raw.choices
    assert raw.choices[0].message.content
