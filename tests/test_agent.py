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
    CONTEXT_TRIM_TARGET_TOKENS,
    MAX_INPUT_TOKENS,
    BackshopAgent,
    _estimate_tokens,
)
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
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
    """_estimate_tokens should return a char-based token count with per-message overhead."""
    messages = [
        {"role": "system", "content": "Hello world"},  # 11 chars / 3.5 = 3 + 4 overhead = 7
        {"role": "user", "content": "How are you?"},  # 12 chars / 3.5 = 3 + 4 overhead = 7
    ]
    result = _estimate_tokens(messages)
    # int(11/3.5) + 4 + int(12/3.5) + 4 = 3 + 4 + 3 + 4 = 14
    assert result == 14


def test_estimate_tokens_handles_empty_messages() -> None:
    """_estimate_tokens should handle empty content, counting only overhead."""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": ""},
        {"role": "user"},  # no content key at all
    ]
    result = _estimate_tokens(messages)
    # 2 messages x 4 overhead tokens each = 8
    assert result == 8


def test_estimate_tokens_handles_empty_list() -> None:
    """_estimate_tokens should return 0 for an empty message list."""
    assert _estimate_tokens([]) == 0


def test_estimate_tokens_counts_tool_call_content() -> None:
    """_estimate_tokens should include tool_calls function names and arguments."""
    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "save_fact",
                        "arguments": '{"key": "rate", "value": "$75/hr"}',
                    },
                }
            ],
        },
    ]
    result = _estimate_tokens(messages)

    # Overhead: 4 tokens
    # content is None -> 0
    # tool_calls: "save_fact" = 9 chars -> int(9/3.5) = 2
    #   arguments = 34 chars -> int(34/3.5) = 9
    # Total = 4 + 0 + 2 + 9 = 15
    assert result == 15

    # Compare with a message that has no tool_calls -- should be less
    plain = [{"role": "assistant", "content": None}]
    assert _estimate_tokens(plain) < result


def test_trim_messages_preserves_tool_call_result_pairs() -> None:
    """Trimming should never orphan a tool result by removing its tool_call."""
    system = {"role": "system", "content": "x" * 3500}
    user1 = {"role": "user", "content": "x" * 3500}
    assistant_tc = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "save_fact", "arguments": "{}"},
            }
        ],
    }
    tool_result = {"role": "tool", "tool_call_id": "call_1", "content": "x" * 3500}
    user2 = {"role": "user", "content": "Final question"}

    messages: list[dict[str, object]] = [
        system,
        user1,
        assistant_tc,
        tool_result,
        user2,
    ]

    # Use a small budget that forces trimming of some messages
    trimmed = BackshopAgent._trim_messages(messages, target_tokens=5000)

    # The trimmed result should never contain tool_result without assistant_tc
    has_tool_msg = any(m.get("role") == "tool" for m in trimmed)
    has_tc_msg = any(m.get("role") == "assistant" and m.get("tool_calls") for m in trimmed)

    if has_tool_msg:
        assert has_tc_msg, "Tool result present without its tool_call assistant message"
    if has_tc_msg:
        assert has_tool_msg, "Tool call assistant message present without its tool result"


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

    # Verify the retry call used trimmed messages within the token budget
    retry_call = mock_acompletion.call_args_list[1]
    retry_messages = retry_call.kwargs["messages"]
    assert _estimate_tokens(retry_messages) <= CONTEXT_TRIM_TARGET_TOKENS
    # System prompt should always be preserved
    assert retry_messages[0]["role"] == "system"


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
    """Long conversations should be trimmed to fit within the token budget."""
    # Each message: ~1143 content tokens + 4 overhead = ~1147 tokens
    # With a small budget, most messages should be trimmed
    big_content = "x" * 4000
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "System prompt"},
        *[
            {"role": "user" if i % 2 == 0 else "assistant", "content": big_content}
            for i in range(20)
        ],
    ]
    # Use a small token budget to force trimming
    trimmed = BackshopAgent._trim_messages(messages, target_tokens=5000)
    assert trimmed[0]["role"] == "system"
    # Should have been trimmed significantly
    assert len(trimmed) < len(messages)
    # Last message should be the most recent one
    assert trimmed[-1] == messages[-1]
    # Should fit within the target budget
    assert _estimate_tokens(trimmed) <= 5000


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
    assert "[Check the expected parameter format" in content


# ---------------------------------------------------------------------------
# Structured error taxonomy tests (issue #299)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_kind_not_found_produces_specific_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """NOT_FOUND error kind should produce a resource-not-found hint."""

    async def missing_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: item #42 not found",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    tool = Tool(name="find_item", description="test", function=missing_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "find_item", "arguments": json.dumps({})}]
        ),
        make_text_response("That item doesn't exist."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    assert "not found" in result_content.lower()
    assert "[The requested resource was not found" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_kind_service_produces_specific_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """SERVICE error kind should produce an external-service hint."""

    async def failing_service(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: Dropbox API unavailable",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )

    tool = Tool(name="upload", description="test", function=failing_service, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "upload", "arguments": json.dumps({})}]
        ),
        make_text_response("Storage is down."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    assert "[An external service is temporarily unavailable" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_kind_validation_produces_specific_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """VALIDATION error kind should produce a parameter-check hint."""

    async def bad_args_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: quantity must be positive",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    tool = Tool(name="create_thing", description="test", function=bad_args_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "create_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("Let me fix that."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    assert "[Check the expected parameter format" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_kind_internal_produces_specific_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """INTERNAL error kind should produce a do-not-retry hint."""

    async def buggy_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: unexpected None in config",
            is_error=True,
            error_kind=ToolErrorKind.INTERNAL,
        )

    tool = Tool(name="broken_tool", description="test", function=buggy_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "broken_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Something went wrong."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    assert "[An internal error occurred" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_with_no_kind_uses_default_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """ToolResult with is_error=True but no error_kind should use the default hint."""

    async def legacy_error_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Error: something went wrong", is_error=True)

    tool = Tool(name="legacy_tool", description="test", function=legacy_error_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "legacy_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("I'll try another way."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    assert "[Analyze the error above and try a different approach.]" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_error_with_custom_hint_overrides_kind_default(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """ToolResult with a custom hint should use it instead of the error_kind default."""

    async def custom_hint_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: estimate #99 not found",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
            hint="Ask the user for the correct estimate number.",
        )

    tool = Tool(name="get_estimate", description="test", function=custom_hint_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "get_estimate", "arguments": json.dumps({})}]
        ),
        make_text_response("Which estimate?"),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0]["result"]
    # Custom hint should appear, not the default NOT_FOUND hint
    assert "[Ask the user for the correct estimate number.]" in result_content
    assert "requested resource was not found" not in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_different_error_kinds_produce_different_hints(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Each error kind should produce a distinct guidance message."""
    collected_hints: dict[str, str] = {}

    for kind in ToolErrorKind:

        async def make_tool(error_kind: ToolErrorKind = kind, **kwargs: object) -> ToolResult:
            return ToolResult(
                content=f"Error: {error_kind.value} failure",
                is_error=True,
                error_kind=error_kind,
            )

        tool = Tool(name="test_tool", description="test", function=make_tool, parameters={})

        mock_acompletion.reset_mock()
        mock_acompletion.side_effect = [
            make_tool_call_response(
                tool_calls=[{"id": "call_1", "name": "test_tool", "arguments": json.dumps({})}]
            ),
            make_text_response("ok"),
        ]

        agent = BackshopAgent(db=db_session, contractor=test_contractor)
        agent.register_tools([tool])
        response = await agent.process_message("test", system_prompt_override="system")

        collected_hints[kind.value] = response.tool_calls[0]["result"]

    # All hints should be different from each other
    hint_values = list(collected_hints.values())
    assert len(set(hint_values)) == len(hint_values), (
        f"Expected all error kinds to produce unique hints, got: {collected_hints}"
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_unhandled_exception_uses_internal_hint(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Unhandled tool exceptions should produce INTERNAL error kind hint."""

    async def crashing_tool(**kwargs: object) -> ToolResult:
        raise RuntimeError("Unexpected crash")

    tool = Tool(name="crash_tool", description="test", function=crashing_tool, parameters={})

    mock_acompletion.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "crash_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Something went wrong."),
    ]

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM
    followup_call = mock_acompletion.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool")
    content = tool_msg["content"]

    assert "[An internal error occurred" in content
