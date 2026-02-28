import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.tools.base import Tool
from backend.app.models import Contractor
from tests.mocks.llm import make_text_response, make_tool_call_response


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_responds_to_message(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should produce a reply from LLM response."""
    mock_acompletion.return_value = make_text_response("Sure, I can help with that deck estimate!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("I need a quote for a 12x12 composite deck")

    assert response.reply_text == "Sure, I can help with that deck estimate!"
    mock_acompletion.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_includes_conversation_history(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should include conversation history in LLM call."""
    mock_acompletion.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    history = [
        {"role": "user", "content": "Hi, I need help"},
        {"role": "assistant", "content": "Hello! How can I help?"},
    ]
    await agent.process_message("What about a deck?", conversation_history=history)

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    # system + 2 history + 1 current = 4
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "Hi, I need help"
    assert messages[3]["content"] == "What about a deck?"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_system_prompt_includes_soul(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent system prompt should include contractor profile info."""
    mock_acompletion.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["messages"][0]["content"]
    assert test_contractor.name in system_prompt


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_does_not_pass_api_key(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """acompletion should be called without api_key so the SDK resolves keys from env."""
    mock_acompletion.return_value = make_text_response("Hi!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert "api_key" not in call_args.kwargs


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_tool_loop_sends_results_back(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """After tool calls, agent should send results back to LLM for a follow-up response."""
    # First call: LLM requests a tool call
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "name": "save_fact",
                "arguments": json.dumps({"key": "hourly_rate", "value": "$75/hr"}),
            }
        ]
    )
    # Second call: LLM produces the final reply
    followup_response = make_text_response("Got it, I'll remember your rate is $75/hour!")

    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    # Register a mock save_fact tool
    mock_save = AsyncMock(return_value="Saved hourly_rate = $75/hr")
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        parameters={"type": "object", "properties": {"key": {}, "value": {}}},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $75/hour")

    # Verify the tool was called
    mock_save.assert_called_once_with(key="hourly_rate", value="$75/hr")

    # Verify follow-up LLM call was made (2 calls total)
    assert mock_acompletion.call_count == 2  # type: ignore[union-attr]

    # Verify the reply comes from the follow-up response, not "Done."
    assert response.reply_text == "Got it, I'll remember your rate is $75/hour!"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["name"] == "save_fact"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_tool_loop_includes_tool_results_in_followup(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Follow-up LLM call should include tool result messages."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_abc",
                "name": "recall_facts",
                "arguments": json.dumps({"query": "hourly rate"}),
            }
        ]
    )
    followup_response = make_text_response("Your hourly rate is $75.")

    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value="hourly_rate: $75/hr")
    tool = Tool(
        name="recall_facts",
        description="Recall facts",
        function=mock_recall,
        parameters={"type": "object", "properties": {"query": {}}},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    await agent.process_message("What's my rate?")

    # Verify the follow-up call includes tool result messages
    followup_call = mock_acompletion.call_args_list[1]  # type: ignore[union-attr]
    messages = followup_call.kwargs["messages"]

    # Should have: system, user, assistant (with tool_calls), tool result
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_abc"
    assert "hourly_rate: $75/hr" in tool_messages[0]["content"]
