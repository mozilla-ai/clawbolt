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
@patch("backend.app.agent.core.settings")
@patch("backend.app.agent.core.acompletion")
async def test_agent_passes_user_parameter(
    mock_acompletion: object,
    mock_settings: object,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """acompletion should be called with user=contractor.id when provider is openai."""
    mock_acompletion.return_value = make_text_response("Hi!")  # type: ignore[union-attr]
    mock_settings.llm_provider = "openai"  # type: ignore[attr-defined]
    mock_settings.llm_model = "gpt-4o"  # type: ignore[attr-defined]
    mock_settings.llm_api_base = None  # type: ignore[attr-defined]
    mock_settings.llm_max_tokens_agent = 500  # type: ignore[attr-defined]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert call_args.kwargs["user"] == str(test_contractor.id)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.settings")
@patch("backend.app.agent.core.acompletion")
async def test_agent_omits_user_for_non_openai_provider(
    mock_acompletion: object,
    mock_settings: object,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """acompletion should NOT pass user param when provider is not openai (e.g. anthropic)."""
    mock_acompletion.return_value = make_text_response("Hi!")  # type: ignore[union-attr]
    mock_settings.llm_provider = "anthropic"  # type: ignore[attr-defined]
    mock_settings.llm_model = "claude-haiku-4-5-20251001"  # type: ignore[attr-defined]
    mock_settings.llm_api_base = None  # type: ignore[attr-defined]
    mock_settings.llm_max_tokens_agent = 500  # type: ignore[attr-defined]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert "user" not in call_args.kwargs


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


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_multi_round_tool_calls(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should support multiple rounds of tool calls, not just one."""
    # Round 1: LLM calls recall_facts
    round1_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_1",
                "name": "recall_facts",
                "arguments": json.dumps({"query": "deck pricing"}),
            }
        ]
    )
    # Round 2: LLM calls generate_estimate
    round2_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_2",
                "name": "generate_estimate",
                "arguments": json.dumps({"description": "deck build"}),
            }
        ]
    )
    # Round 3: LLM produces final text reply
    final_response = make_text_response("Here's your estimate for the deck build!")

    mock_acompletion.side_effect = [round1_response, round2_response, final_response]  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value="deck: $45/sqft")
    mock_estimate = AsyncMock(return_value="Estimate PDF generated")

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools(
        [
            Tool(
                name="recall_facts",
                description="Recall facts",
                function=mock_recall,
                parameters={"type": "object", "properties": {"query": {}}},
            ),
            Tool(
                name="generate_estimate",
                description="Generate estimate",
                function=mock_estimate,
                parameters={"type": "object", "properties": {"description": {}}},
            ),
        ]
    )

    response = await agent.process_message("Look up deck pricing and generate an estimate")

    # Both tools should have been called
    mock_recall.assert_called_once()
    mock_estimate.assert_called_once()

    # 3 LLM calls total (round 1 + round 2 + final)
    assert mock_acompletion.call_count == 3  # type: ignore[union-attr]

    # Final reply comes from the text response
    assert response.reply_text == "Here's your estimate for the deck build!"

    # Both tool calls should be recorded
    assert len(response.tool_calls) == 2
    assert response.tool_calls[0]["name"] == "recall_facts"
    assert response.tool_calls[1]["name"] == "generate_estimate"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_tool_loop_respects_max_rounds(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should stop after MAX_TOOL_ROUNDS even if LLM keeps requesting tools."""
    from backend.app.agent.core import MAX_TOOL_ROUNDS

    # Create MAX_TOOL_ROUNDS responses that all request tool calls
    tool_responses = [
        make_tool_call_response(
            tool_calls=[
                {
                    "id": f"call_{i}",
                    "name": "recall_facts",
                    "arguments": json.dumps({"query": f"round {i}"}),
                }
            ],
            content="Still thinking...",
        )
        for i in range(MAX_TOOL_ROUNDS)
    ]

    mock_acompletion.side_effect = tool_responses  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value="some result")
    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools(
        [
            Tool(
                name="recall_facts",
                description="Recall facts",
                function=mock_recall,
                parameters={"type": "object", "properties": {"query": {}}},
            ),
        ]
    )

    response = await agent.process_message("Keep going forever")

    # Should have made exactly MAX_TOOL_ROUNDS calls, not more
    assert mock_acompletion.call_count == MAX_TOOL_ROUNDS  # type: ignore[union-attr]

    # Should still return a reply (from the last response's content)
    assert response.reply_text == "Still thinking..."


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_handles_malformed_tool_arguments(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should gracefully handle malformed JSON in tool call arguments."""
    # LLM returns a tool call with invalid JSON arguments
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_bad",
                "name": "save_fact",
                "arguments": "{invalid json!!!",
            }
        ]
    )
    followup_response = make_text_response("Sorry, I had trouble with that.")

    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_save = AsyncMock(return_value="saved")
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        parameters={"type": "object", "properties": {"key": {}, "value": {}}},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $75/hour")

    # Tool should NOT have been called (args were unparseable)
    mock_save.assert_not_called()

    # Agent should still produce a reply (not crash)
    assert response.reply_text == "Sorry, I had trouble with that."

    # The failure should be recorded in actions_taken
    assert any("bad args" in a for a in response.actions_taken)
