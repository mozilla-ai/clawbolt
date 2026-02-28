"""Integration tests that exercise the real acompletion() call path.

These tests require a local LM Studio server running on port 1234.
They are skipped by default and only run via ``pytest -m integration``.

Run locally:
    1. Start LM Studio and load a model
    2. uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.models import Contractor

from .conftest import _LMSTUDIO_URL, skip_without_lmstudio


@pytest.mark.integration()
@skip_without_lmstudio
async def test_agent_returns_nonempty_reply(
    integration_db: Session,
    integration_contractor: Contractor,
    lmstudio_model: str,
) -> None:
    """BackshopAgent.process_message() should return a non-empty reply from a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "lmstudio"
        mock_settings.llm_model = lmstudio_model
        mock_settings.llm_api_base = _LMSTUDIO_URL

        agent = BackshopAgent(db=integration_db, contractor=integration_contractor)
        response = await agent.process_message(
            "Hello, can you help me with a deck estimate?",
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response.reply_text
    assert len(response.reply_text) > 0


@pytest.mark.integration()
@skip_without_lmstudio
async def test_agent_message_format_accepted(
    integration_db: Session,
    integration_contractor: Contractor,
    lmstudio_model: str,
) -> None:
    """The full system prompt + conversation history format should be accepted by a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "lmstudio"
        mock_settings.llm_model = lmstudio_model
        mock_settings.llm_api_base = _LMSTUDIO_URL

        agent = BackshopAgent(db=integration_db, contractor=integration_contractor)
        history = [
            {"role": "user", "content": "Hi there"},
            {"role": "assistant", "content": "Hello! How can I help?"},
        ]
        response = await agent.process_message(
            "What's a fair price for a 10x10 deck?",
            conversation_history=history,
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response.reply_text
    assert len(response.reply_text) > 0


@pytest.mark.integration()
@skip_without_lmstudio
async def test_acompletion_direct_call(lmstudio_model: str) -> None:
    """Verify acompletion() works directly with lmstudio provider."""
    from any_llm import acompletion

    response = await acompletion(
        model=lmstudio_model,
        provider="lmstudio",
        api_base=_LMSTUDIO_URL,
        messages=[
            {"role": "system", "content": "Reply with exactly: HELLO"},
            {"role": "user", "content": "Say hello"},
        ],
        max_tokens=50,
    )

    assert response.choices
    assert response.choices[0].message.content
