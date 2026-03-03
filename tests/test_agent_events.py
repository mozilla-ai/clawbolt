"""Tests for agent event system."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.models import Contractor
from tests.mocks.llm import make_text_response, make_tool_call_response


@pytest.fixture()
def agent(db_session: Session, test_contractor: Contractor) -> BackshopAgent:
    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    return agent


@pytest.mark.asyncio
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.core.build_agent_system_prompt", new_callable=AsyncMock)
async def test_events_emitted_for_text_response(
    mock_prompt: AsyncMock,
    mock_llm: AsyncMock,
    agent: BackshopAgent,
) -> None:
    """Text-only response should emit start, turn_start, turn_end, and end events."""
    mock_prompt.return_value = "system prompt"
    mock_llm.return_value = make_text_response("Hello!")

    events: list[object] = []
    subscriber = AsyncMock(side_effect=lambda e: events.append(e))
    agent.subscribe(subscriber)

    await agent.process_message("Hi there")

    assert len(events) == 4
    assert isinstance(events[0], AgentStartEvent)
    assert events[0].message_context == "Hi there"
    assert isinstance(events[1], TurnStartEvent)
    assert events[1].round_number == 0
    assert isinstance(events[2], TurnEndEvent)
    assert events[2].has_more_tool_calls is False
    assert isinstance(events[3], AgentEndEvent)
    assert events[3].reply_text == "Hello!"
    assert events[3].total_duration_ms > 0


@pytest.mark.asyncio
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.core.build_agent_system_prompt", new_callable=AsyncMock)
async def test_events_emitted_for_tool_call(
    mock_prompt: AsyncMock,
    mock_llm: AsyncMock,
    agent: BackshopAgent,
) -> None:
    """Tool call should emit tool execution start/end events."""
    mock_prompt.return_value = "system prompt"

    async def mock_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="saved")

    agent.register_tools(
        [
            Tool(
                name="save_fact",
                description="Save a fact",
                function=mock_tool,
                parameters={"type": "object", "properties": {}},
            )
        ]
    )

    # First call: tool call, second call: text response
    tool_response = make_tool_call_response(
        [{"name": "save_fact", "arguments": json.dumps({"key": "name", "value": "Mike"})}]
    )
    text_response = make_text_response("Done!")
    mock_llm.side_effect = [tool_response, text_response]

    events: list[object] = []
    subscriber = AsyncMock(side_effect=lambda e: events.append(e))
    agent.subscribe(subscriber)

    await agent.process_message("Remember my name is Mike")

    event_types = [type(e).__name__ for e in events]
    assert "AgentStartEvent" in event_types
    assert "TurnStartEvent" in event_types
    assert "ToolExecutionStartEvent" in event_types
    assert "ToolExecutionEndEvent" in event_types
    assert "TurnEndEvent" in event_types
    assert "AgentEndEvent" in event_types

    # Find tool execution events
    tool_starts = [e for e in events if isinstance(e, ToolExecutionStartEvent)]
    tool_ends = [e for e in events if isinstance(e, ToolExecutionEndEvent)]
    assert len(tool_starts) == 1
    assert tool_starts[0].tool_name == "save_fact"
    assert len(tool_ends) == 1
    assert tool_ends[0].tool_name == "save_fact"
    assert tool_ends[0].is_error is False
    assert tool_ends[0].duration_ms >= 0


@pytest.mark.asyncio
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.core.build_agent_system_prompt", new_callable=AsyncMock)
async def test_no_events_without_subscribers(
    mock_prompt: AsyncMock,
    mock_llm: AsyncMock,
    agent: BackshopAgent,
) -> None:
    """Without subscribers, no errors should occur."""
    mock_prompt.return_value = "system prompt"
    mock_llm.return_value = make_text_response("Hello!")

    # No subscriber registered -- should work fine
    response = await agent.process_message("Hi")
    assert response.reply_text == "Hello!"


@pytest.mark.asyncio
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.core.build_agent_system_prompt", new_callable=AsyncMock)
async def test_subscriber_error_does_not_crash_agent(
    mock_prompt: AsyncMock,
    mock_llm: AsyncMock,
    agent: BackshopAgent,
) -> None:
    """A failing subscriber should not crash the agent pipeline."""
    mock_prompt.return_value = "system prompt"
    mock_llm.return_value = make_text_response("Hello!")

    async def bad_subscriber(event: object) -> None:
        raise RuntimeError("subscriber crashed")

    agent.subscribe(bad_subscriber)

    # Should not raise
    response = await agent.process_message("Hi")
    assert response.reply_text == "Hello!"


@pytest.mark.asyncio
@patch("backend.app.agent.core.acompletion")
@patch("backend.app.agent.core.build_agent_system_prompt", new_callable=AsyncMock)
async def test_multiple_subscribers(
    mock_prompt: AsyncMock,
    mock_llm: AsyncMock,
    agent: BackshopAgent,
) -> None:
    """Multiple subscribers should all receive events."""
    mock_prompt.return_value = "system prompt"
    mock_llm.return_value = make_text_response("Hello!")

    events_a: list[object] = []
    events_b: list[object] = []
    agent.subscribe(AsyncMock(side_effect=lambda e: events_a.append(e)))
    agent.subscribe(AsyncMock(side_effect=lambda e: events_b.append(e)))

    await agent.process_message("Hi")

    assert len(events_a) == 4
    assert len(events_b) == 4


def test_event_dataclasses_are_frozen() -> None:
    """Event dataclasses should be immutable."""
    event = AgentStartEvent(contractor_id=1, message_context="test")
    try:
        event.contractor_id = 2  # type: ignore[misc]
        frozen = False
    except AttributeError:
        frozen = True
    assert frozen
