"""Integration tests for the heartbeat evaluator against a real LLM.

Verifies that evaluate_heartbeat_need() returns valid JSON from a real
model and that the full heartbeat pipeline handles real LLM responses.

Requires ANTHROPIC_API_KEY set in environment:
    ANTHROPIC_API_KEY=sk-... uv run pytest -m integration -v --timeout=120
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.heartbeat import evaluate_heartbeat_need
from backend.app.models import Contractor, Conversation, Estimate, Message

from .conftest import _ANTHROPIC_MODEL, skip_without_anthropic_key


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_heartbeat_evaluate_returns_valid_action(
    integration_db: Session,
    onboarded_contractor: Contractor,
) -> None:
    """evaluate_heartbeat_need() should return a valid HeartbeatAction from a real LLM."""
    with patch("backend.app.agent.heartbeat.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.llm_max_tokens_heartbeat = 300

        action = await evaluate_heartbeat_need(
            integration_db, onboarded_contractor, ["Test flag: check-in needed"]
        )

    assert action.action_type in ("send_message", "no_action")
    assert isinstance(action.reasoning, str)
    assert isinstance(action.priority, int)
    assert 0 <= action.priority <= 5


@pytest.mark.integration()
@skip_without_anthropic_key
async def test_heartbeat_evaluate_with_context(
    integration_db: Session,
    onboarded_contractor: Contractor,
) -> None:
    """Heartbeat should handle a contractor with real conversation history and pending estimates."""
    # Set up conversation with messages
    conv = Conversation(contractor_id=onboarded_contractor.id, is_active=True)
    integration_db.add(conv)
    integration_db.commit()
    integration_db.refresh(conv)

    integration_db.add(
        Message(conversation_id=conv.id, direction="inbound", body="I need a quote for a deck")
    )
    integration_db.add(
        Message(
            conversation_id=conv.id,
            direction="outbound",
            body="Sure! What size deck are you looking at?",
        )
    )

    # Add a pending draft estimate
    integration_db.add(
        Estimate(
            contractor_id=onboarded_contractor.id,
            description="12x12 composite deck build",
            total_amount=4500,
            status="draft",
        )
    )
    integration_db.commit()

    with patch("backend.app.agent.heartbeat.settings") as mock_settings:
        mock_settings.llm_provider = "anthropic"
        mock_settings.llm_model = _ANTHROPIC_MODEL
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_provider = None
        mock_settings.heartbeat_model = None
        mock_settings.llm_max_tokens_heartbeat = 300

        action = await evaluate_heartbeat_need(
            integration_db,
            onboarded_contractor,
            ["Stale draft estimate: 12x12 composite deck build"],
        )

    assert action.action_type in ("send_message", "no_action")
    # If LLM decides to send, the message should be non-empty
    if action.action_type == "send_message":
        assert len(action.message) > 0
