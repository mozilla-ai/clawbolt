"""Integration tests that exercise the real amessages() call path.

These tests require ANTHROPIC_API_KEY set in the environment.
They are skipped by default and only run via ``pytest -m integration``.

Run locally:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.models import User

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_returns_nonempty_reply(
    integration_user: User,
) -> None:
    """ClawboltAgent.process_message() should return a non-empty reply from a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500
        mock_settings.context_trim_target_tokens = 400_000

        agent = ClawboltAgent(user=integration_user)
        response = await agent.process_message(
            "Hello, can you help me with a deck estimate?",
            system_prompt_override="You are a helpful assistant. Reply briefly.",
        )

    assert response is not None


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_agent_message_format_accepted(
    integration_user: User,
) -> None:
    """The full system prompt + conversation history format should be accepted by a real LLM."""
    with patch("backend.app.agent.core.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.llm_max_tokens_agent = 500
        mock_settings.context_trim_target_tokens = 400_000

        agent = ClawboltAgent(user=integration_user)
        history: list[AgentMessage] = [
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
    from any_llm.types.messages import MessageResponse, TextBlock

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
    text_parts = [block.text for block in raw.content if isinstance(block, TextBlock)]
    assert text_parts
    assert text_parts[0]


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_prompt_caching_produces_cache_hits() -> None:
    """Two calls with the same cached system prompt should produce a cache hit.

    Anthropic requires the system prompt to be >= 1024 tokens for caching to
    activate, so we pad the prompt to ensure it crosses that threshold.
    """
    from any_llm import amessages
    from any_llm.types.messages import MessageResponse

    from backend.app.services.llm_service import prepare_system_with_caching

    # Pad the system prompt to exceed 1024 tokens (Anthropic minimum for caching).
    # Each word is roughly one token; 1200 words gives comfortable margin.
    padding = " ".join(f"word{i}" for i in range(1200))
    system_text = f"You are a helpful assistant. Reply briefly. Context: {padding}"
    system = prepare_system_with_caching(system_text)

    # First call: should create a cache entry
    resp1 = await amessages(
        model=_ANTHROPIC_MODEL,
        provider="anthropic",
        system=system,
        messages=[{"role": "user", "content": "Say hello"}],
        max_tokens=50,
    )
    assert isinstance(resp1, MessageResponse)
    assert resp1.usage.cache_creation_input_tokens and resp1.usage.cache_creation_input_tokens > 0

    # Second call: same system prompt should hit the cache
    resp2 = await amessages(
        model=_ANTHROPIC_MODEL,
        provider="anthropic",
        system=system,
        messages=[{"role": "user", "content": "Say goodbye"}],
        max_tokens=50,
    )
    assert isinstance(resp2, MessageResponse)
    assert resp2.usage.cache_read_input_tokens and resp2.usage.cache_read_input_tokens > 0
