"""Integration tests for the heartbeat evaluator against a real LLM.

Verifies that evaluate_heartbeat_need() returns valid JSON from a real
model and that the full heartbeat pipeline handles real LLM responses.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest

from backend.app.agent.client_db import EstimateStore
from backend.app.agent.dto import UserData
from backend.app.agent.heartbeat import evaluate_heartbeat_need
from backend.app.agent.session_db import get_session_store

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_heartbeat_evaluate_returns_valid_action(
    onboarded_user: UserData,
) -> None:
    """evaluate_heartbeat_need() should return a valid HeartbeatDecision from a real LLM."""
    with patch("backend.app.agent.heartbeat.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.llm_max_tokens_heartbeat = 300
        mock_settings.heartbeat_recent_messages_count = 5

        decision = await evaluate_heartbeat_need(onboarded_user)

    assert decision.action in ("skip", "run")
    assert isinstance(decision.reasoning, str)
    assert isinstance(decision.tasks, str)


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_heartbeat_evaluate_with_context(
    onboarded_user: UserData,
) -> None:
    """Heartbeat should handle a user with real conversation history and pending estimates."""
    # Set up conversation with messages via file store
    session_store = get_session_store(onboarded_user.id)
    session, _ = await session_store.get_or_create_session()

    await session_store.add_message(
        session,
        direction="inbound",
        body="I need a quote for a deck",
    )
    await session_store.add_message(
        session,
        direction="outbound",
        body="Sure! What size deck are you looking at?",
    )

    # Add a pending draft estimate
    estimate_store = EstimateStore(onboarded_user.id)
    await estimate_store.create(
        description="12x12 composite deck build",
        total_amount=4500,
        status="draft",
    )

    with patch("backend.app.agent.heartbeat.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.llm_max_tokens_heartbeat = 300
        mock_settings.heartbeat_recent_messages_count = 5

        decision = await evaluate_heartbeat_need(onboarded_user)

    assert decision.action in ("skip", "run")
    # If LLM decides to run, the tasks should be non-empty
    if decision.action == "run":
        assert len(decision.tasks) > 0
