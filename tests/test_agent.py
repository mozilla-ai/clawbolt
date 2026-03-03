import json
from unittest.mock import AsyncMock, patch

import pytest
from any_llm import (
    AuthenticationError,
    ContentFilterError,
    ContextLengthExceededError,
    RateLimitError,
)
from sqlalchemy.orm import Session

from backend.app.agent.core import (
    CONTEXT_TRIM_KEEP_RECENT,
    MAX_INPUT_TOKENS,
    BackshopAgent,
    _estimate_tokens,
)
from backend.app.agent.tools.base import Tool, ToolResult
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
async def test_agent_always_passes_user_parameter(
    mock_acompletion: object,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """acompletion should always be called with user=contractor.id regardless of provider."""
    mock_acompletion.return_value = make_text_response("Hi!")  # type: ignore[union-attr]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message("Hello")

    call_args = mock_acompletion.call_args  # type: ignore[union-attr]
    assert call_args.kwargs["user"] == str(test_contractor.id)


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


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_repairs_slightly_malformed_json(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should repair common LLM JSON mistakes like trailing commas."""
    # LLM returns a tool call with a trailing comma (common LLM mistake)
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_repair",
                "name": "save_fact",
                "arguments": '{"key": "hourly_rate", "value": "$75/hr",}',
            }
        ]
    )
    followup_response = make_text_response("Got it!")

    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

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

    # Tool SHOULD have been called despite the trailing comma
    mock_save.assert_called_once_with(key="hourly_rate", value="$75/hr")
    assert any("Called save_fact" in a for a in response.actions_taken)


# ---------------------------------------------------------------------------
# Typed LLM exception handling tests (issue #173)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.asyncio.sleep", new_callable=AsyncMock)
@patch("backend.app.agent.core.acompletion")
async def test_agent_retries_on_rate_limit_error(
    mock_acompletion: AsyncMock,
    mock_sleep: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """RateLimitError should trigger one retry after a delay."""
    mock_acompletion.side_effect = [
        RateLimitError("Too many requests"),
        make_text_response("Retry succeeded!"),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("Hello")

    assert response.reply_text == "Retry succeeded!"
    assert mock_acompletion.call_count == 2
    mock_sleep.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.asyncio.sleep", new_callable=AsyncMock)
@patch("backend.app.agent.core.acompletion")
async def test_agent_rate_limit_retry_failure_propagates(
    mock_acompletion: AsyncMock,
    mock_sleep: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """If the retry after RateLimitError also fails, the exception propagates."""
    mock_acompletion.side_effect = [
        RateLimitError("Too many requests"),
        RateLimitError("Still rate limited"),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    with pytest.raises(RateLimitError):
        await agent.process_message("Hello")

    assert mock_acompletion.call_count == 2


# ---------------------------------------------------------------------------
# Context window overflow protection tests (issue #172)
# ---------------------------------------------------------------------------


def test_estimate_tokens_returns_reasonable_estimate() -> None:
    """_estimate_tokens should return a rough char-based token count."""
    messages = [
        {"role": "system", "content": "Hello world"},  # 11 chars -> 2 tokens
        {"role": "user", "content": "How are you?"},  # 12 chars -> 3 tokens
    ]
    result = _estimate_tokens(messages)
    # 11 // 4 + 12 // 4 = 2 + 3 = 5
    assert result == 5


def test_estimate_tokens_handles_empty_messages() -> None:
    """_estimate_tokens should handle empty content gracefully."""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": ""},
        {"role": "user"},  # no content key at all
    ]
    result = _estimate_tokens(messages)
    assert result == 0


def test_estimate_tokens_handles_empty_list() -> None:
    """_estimate_tokens should return 0 for an empty message list."""
    assert _estimate_tokens([]) == 0


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_trims_context_on_context_length_exceeded(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """ContextLengthExceededError should trim messages and retry once."""
    mock_acompletion.side_effect = [
        ContextLengthExceededError("Input too long"),
        make_text_response("Trimmed and retried!"),
    ]

    # Supply a long conversation history to verify trimming
    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"}
        for i in range(20)
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("Current message", conversation_history=long_history)

    assert response.reply_text == "Trimmed and retried!"
    assert mock_acompletion.call_count == 2

    # Verify the retry call used trimmed messages
    retry_call = mock_acompletion.call_args_list[1]
    retry_messages = retry_call.kwargs["messages"]
    # Should be system + CONTEXT_TRIM_KEEP_RECENT messages
    assert len(retry_messages) == CONTEXT_TRIM_KEEP_RECENT + 1


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_trims_history_when_exceeding_token_limit(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Messages should be trimmed when estimated tokens exceed MAX_INPUT_TOKENS."""
    mock_acompletion.return_value = make_text_response("Trimmed reply!")

    # Create a huge conversation history that exceeds MAX_INPUT_TOKENS
    # Each message ~4000 chars = ~1000 tokens; need >120K tokens = >120 messages
    big_content = "x" * 4000
    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": big_content} for i in range(150)
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("Current message", conversation_history=long_history)

    assert response.reply_text == "Trimmed reply!"

    # Verify that the messages sent to acompletion were trimmed
    call_args = mock_acompletion.call_args
    messages = call_args.kwargs["messages"]
    total_tokens = _estimate_tokens(messages)
    assert total_tokens <= MAX_INPUT_TOKENS


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_raises_content_filter_error(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """ContentFilterError should be re-raised (handled by router)."""
    mock_acompletion.side_effect = ContentFilterError("Blocked by safety filter")

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    with pytest.raises(ContentFilterError):
        await agent.process_message("Something problematic")

    assert mock_acompletion.call_count == 1


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_preserves_system_and_user_during_trimming(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """System prompt and latest user message must survive trimming."""
    mock_acompletion.return_value = make_text_response("Ok!")

    # Create history that will trigger trimming
    big_content = "x" * 4000
    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": big_content} for i in range(150)
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message(
        "My important question",
        conversation_history=long_history,
        system_prompt_override="Custom system prompt",
    )

    call_args = mock_acompletion.call_args
    messages = call_args.kwargs["messages"]

    # System prompt is always first
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "Custom system prompt"

    # Latest user message is always last
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "My important question"

    # At least 2 messages: system + user
    assert len(messages) >= 2


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_raises_authentication_error(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """AuthenticationError should be re-raised (handled by router)."""
    mock_acompletion.side_effect = AuthenticationError("Invalid API key")

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    with pytest.raises(AuthenticationError):
        await agent.process_message("Hello")

    assert mock_acompletion.call_count == 1


def test_trim_messages_preserves_short_conversation() -> None:
    """Messages shorter than the threshold should be returned unchanged."""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    trimmed = BackshopAgent._trim_messages(messages)
    assert trimmed == messages


def test_trim_messages_keeps_system_and_recent() -> None:
    """Long conversations should be trimmed to system + most recent N messages."""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "System prompt"},
        *[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Msg {i}"}
            for i in range(20)
        ],
    ]
    trimmed = BackshopAgent._trim_messages(messages)
    assert len(trimmed) == CONTEXT_TRIM_KEEP_RECENT + 1
    assert trimmed[0]["role"] == "system"
    # Last message should be the most recent one
    assert trimmed[-1] == messages[-1]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_does_not_trim_normal_conversations(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Normal-sized conversations should not be trimmed."""
    mock_acompletion.return_value = make_text_response("Got it!")

    history = [
        {"role": "user", "content": "Hi, I need help"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "Can you estimate a deck?"},
        {"role": "assistant", "content": "Sure, what size?"},
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    await agent.process_message(
        "12x12 composite deck",
        conversation_history=history,
        system_prompt_override="System prompt",
    )

    call_args = mock_acompletion.call_args
    messages = call_args.kwargs["messages"]

    # system + 4 history + 1 current = 6 — nothing trimmed
    assert len(messages) == 6


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_logs_warning_when_trimming(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning should be logged when conversation history is trimmed."""
    mock_acompletion.return_value = make_text_response("Ok!")

    big_content = "x" * 4000
    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": big_content} for i in range(150)
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)

    with caplog.at_level("WARNING", logger="backend.app.agent.core"):
        await agent.process_message(
            "Current message",
            conversation_history=long_history,
            system_prompt_override="Short system prompt",
        )

    assert any("Trimmed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Dict-based tool registry tests (issue #282)
# ---------------------------------------------------------------------------


def test_register_tools_builds_dict_lookup(
    db_session: Session, test_contractor: Contractor
) -> None:
    """register_tools should build a dict for O(1) lookup by name."""
    agent = BackshopAgent(db=db_session, contractor=test_contractor)

    async def dummy(**kwargs: object) -> str:
        return "ok"

    tools = [
        Tool(name="tool_a", description="A", function=dummy, parameters={}),
        Tool(name="tool_b", description="B", function=dummy, parameters={}),
    ]
    agent.register_tools(tools)

    assert agent._find_tool("tool_a") is not None
    assert agent._find_tool("tool_b") is not None
    assert agent._find_tool("nonexistent") is None


def test_register_tools_warns_on_duplicate_name(
    db_session: Session,
    test_contractor: Contractor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registering tools with duplicate names should log a warning."""
    agent = BackshopAgent(db=db_session, contractor=test_contractor)

    async def dummy(**kwargs: object) -> str:
        return "ok"

    tools = [
        Tool(name="dupe", description="First", function=dummy, parameters={}),
        Tool(name="dupe", description="Second", function=dummy, parameters={}),
    ]

    with caplog.at_level("WARNING", logger="backend.app.agent.core"):
        agent.register_tools(tools)

    assert any("Duplicate tool name" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Structured ToolResult tests (issue #280)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_tool_result_error_appends_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When a tool returns ToolResult(is_error=True), a hint is appended."""

    async def failing_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Error: item not found", is_error=True)

    tool = Tool(name="do_thing", description="test", function=failing_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "do_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("I'll try something else."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # The hint should have been appended to the error result
    assert any("Failed: do_thing" in a for a in response.actions_taken)
    assert response.tool_calls[0]["is_error"] is True
    assert "[Analyze the error" in response.tool_calls[0]["result"]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_tool_result_success_no_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When a tool returns ToolResult(is_error=False), no hint is appended."""

    async def ok_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Done!")

    tool = Tool(name="do_thing", description="test", function=ok_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "do_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("Great!"),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    assert any("Called do_thing" in a for a in response.actions_taken)
    assert response.tool_calls[0]["is_error"] is False
    assert "[Analyze the error" not in response.tool_calls[0]["result"]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_plain_string_return_backward_compat(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Tools returning plain strings should still work (backward compatibility)."""

    async def legacy_tool(**kwargs: object) -> str:
        return "Legacy result"

    tool = Tool(name="old_tool", description="test", function=legacy_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "old_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Ok!"),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    assert any("Called old_tool" in a for a in response.actions_taken)
    assert response.tool_calls[0]["result"] == "Legacy result"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_tool_exception_appends_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When a tool raises an exception, a self-correction hint is appended."""

    async def crashing_tool(**kwargs: object) -> ToolResult:
        raise RuntimeError("Something broke")

    tool = Tool(name="bad_tool", description="test", function=crashing_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "bad_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Let me try another way."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    assert any("Failed: bad_tool" in a for a in response.actions_taken)


# ---------------------------------------------------------------------------
# Tool error feedback tests (issue #292)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_unknown_tool_error_lists_available_tools(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Unknown tool error should list all registered tool names."""

    async def dummy(**kwargs: object) -> str:
        return "ok"

    tools = [
        Tool(name="save_fact", description="Save a fact", function=dummy, parameters={}),
        Tool(name="recall_facts", description="Recall facts", function=dummy, parameters={}),
    ]

    # LLM calls a tool that doesn't exist
    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[
                {"id": "call_1", "name": "save_notes", "arguments": json.dumps({"text": "hi"})}
            ]
        ),
        make_text_response("Let me try again."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools(tools)
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM
    followup_call = mock_acompletion.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    content = tool_msg["content"]

    assert 'unknown tool "save_notes"' in content
    assert "save_fact" in content
    assert "recall_facts" in content
    assert "[Analyze the error" in content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_validation_error_includes_expected_schema(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Validation errors should include the expected parameter schema."""
    from pydantic import BaseModel

    class FactParams(BaseModel):
        key: str
        value: str
        category: str = "general"

    async def save_fact(**kwargs: object) -> str:
        return "saved"

    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=save_fact,
        parameters={},
        params_model=FactParams,
    )

    # LLM calls save_fact with missing required fields
    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[
                {"id": "call_1", "name": "save_fact", "arguments": json.dumps({"category": "job"})}
            ]
        ),
        make_text_response("Let me fix that."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM
    followup_call = mock_acompletion.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    content = tool_msg["content"]

    assert "Validation error for save_fact" in content
    assert "Expected parameters:" in content
    assert '"key": string (required)' in content
    assert '"value": string (required)' in content
    assert '"category": string (optional, default: general)' in content
    assert "[Analyze the error" in content
