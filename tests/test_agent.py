import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from any_llm import (
    AuthenticationError,
    ContentFilterError,
    ContextLengthExceededError,
    RateLimitError,
)
from pydantic import BaseModel

from backend.app.agent.core import (
    ClawboltAgent,
)
from backend.app.agent.file_store import UserData
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.trimming import trim_messages
from tests.mocks.llm import make_error_response, make_text_response, make_tool_call_response


class _EmptyParams(BaseModel):
    """Minimal params model used by registry tests that need a valid tool."""


class _KeyValueParams(BaseModel):
    """Params model for tools accepting key/value pairs."""

    key: str
    value: str


class _QueryParams(BaseModel):
    """Params model for tools accepting a query."""

    query: str


class _DescriptionParams(BaseModel):
    """Params model for tools accepting a description."""

    description: str


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_responds_to_message(mock_amessages: object, test_user: UserData) -> None:
    """Agent should produce a reply from LLM response."""
    mock_amessages.return_value = make_text_response("Sure, I can help with that deck estimate!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    response = await agent.process_message("I need a quote for a 12x12 composite deck")

    assert response.reply_text == "Sure, I can help with that deck estimate!"
    mock_amessages.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_includes_conversation_history(
    mock_amessages: object, test_user: UserData
) -> None:
    """Agent should include conversation history in LLM call."""
    mock_amessages.return_value = make_text_response("Got it!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    history: list[AgentMessage] = [
        UserMessage(content="Hi, I need help"),
        AssistantMessage(content="Hello! How can I help?"),
    ]
    await agent.process_message("What about a deck?", conversation_history=history)

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    messages = call_args.kwargs["messages"]
    # System is extracted to 'system' kwarg; 2 history + 1 current = 3
    assert call_args.kwargs["system"] is not None
    assert len(messages) == 3
    assert messages[0]["content"] == "Hi, I need help"
    assert messages[2]["content"] == "What about a deck?"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_system_prompt_includes_soul(
    mock_amessages: object, test_user: UserData
) -> None:
    """Agent system prompt should include user profile info."""
    mock_amessages.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    await agent.process_message("Hello")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert test_user.name in system_prompt


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_system_prompt_includes_tool_hints(
    mock_amessages: object, test_user: UserData
) -> None:
    """System prompt should include usage hints from registered tools."""
    mock_amessages.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(
            name="save_fact",
            description="Save a fact",
            function=dummy,
            params_model=_EmptyParams,
            usage_hint="When you learn new info, save it.",
        ),
        Tool(
            name="recall_facts",
            description="Recall facts",
            function=dummy,
            params_model=_EmptyParams,
            usage_hint="Search memory for relevant information.",
        ),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)
    await agent.process_message("Hello")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert "Tool Guidelines" in system_prompt
    assert "When you learn new info, save it." in system_prompt
    assert "Search memory for relevant information." in system_prompt


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_system_prompt_omits_tool_section_when_no_hints(
    mock_amessages: object, test_user: UserData
) -> None:
    """System prompt should not include tool guidelines when no tools have hints."""
    mock_amessages.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    # No tools registered
    await agent.process_message("Hello")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert "Tool Guidelines" not in system_prompt


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_system_prompt_skips_tools_without_hints(
    mock_amessages: object, test_user: UserData
) -> None:
    """Tools with empty usage_hint should not appear in the system prompt."""
    mock_amessages.return_value = make_text_response("Ok!")  # type: ignore[union-attr]

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(
            name="tool_with_hint",
            description="Has a hint",
            function=dummy,
            params_model=_EmptyParams,
            usage_hint="This tool does something useful.",
        ),
        Tool(
            name="tool_without_hint",
            description="No hint",
            function=dummy,
            params_model=_EmptyParams,
        ),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)
    await agent.process_message("Hello")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    system_prompt = call_args.kwargs["system"]
    assert "This tool does something useful." in system_prompt
    assert "tool_without_hint" not in system_prompt


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_does_not_pass_api_key(mock_amessages: object, test_user: UserData) -> None:
    """acompletion should be called without api_key so the SDK resolves keys from env."""
    mock_amessages.return_value = make_text_response("Hi!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    await agent.process_message("Hello")

    call_args = mock_amessages.call_args  # type: ignore[union-attr]
    assert "api_key" not in call_args.kwargs


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_tool_loop_sends_results_back(
    mock_amessages: object, test_user: UserData
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

    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    # Register a mock save_fact tool
    mock_save = AsyncMock(return_value=ToolResult(content="Saved hourly_rate = $75/hr"))
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        params_model=_KeyValueParams,
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $75/hour")

    # Verify the tool was called
    mock_save.assert_called_once_with(key="hourly_rate", value="$75/hr")

    # Verify follow-up LLM call was made (2 calls total)
    assert mock_amessages.call_count == 2  # type: ignore[union-attr]

    # Verify the reply comes from the follow-up response, not "Done."
    assert response.reply_text == "Got it, I'll remember your rate is $75/hour!"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "save_fact"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_tool_loop_includes_tool_results_in_followup(
    mock_amessages: object, test_user: UserData
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

    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value=ToolResult(content="hourly_rate: $75/hr"))
    tool = Tool(
        name="recall_facts",
        description="Recall facts",
        function=mock_recall,
        params_model=_QueryParams,
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    await agent.process_message("What's my rate?")

    # Verify the follow-up call includes tool result messages
    followup_call = mock_amessages.call_args_list[1]  # type: ignore[union-attr]
    messages = followup_call.kwargs["messages"]

    # Should have: user, assistant (with tool_use), user (with tool_result)
    # Tool results are sent as user messages with tool_result content blocks
    tool_result_msgs = [
        m for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    assert len(tool_result_msgs) == 1
    tool_result_block = tool_result_msgs[0]["content"][0]
    assert tool_result_block["tool_use_id"] == "call_abc"
    assert "hourly_rate: $75/hr" in tool_result_block["content"]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_multi_round_tool_calls(mock_amessages: object, test_user: UserData) -> None:
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

    mock_amessages.side_effect = [round1_response, round2_response, final_response]  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value=ToolResult(content="deck: $45/sqft"))
    mock_estimate = AsyncMock(return_value=ToolResult(content="Estimate PDF generated"))

    agent = ClawboltAgent(user=test_user)
    agent.register_tools(
        [
            Tool(
                name="recall_facts",
                description="Recall facts",
                function=mock_recall,
                params_model=_QueryParams,
            ),
            Tool(
                name="generate_estimate",
                description="Generate estimate",
                function=mock_estimate,
                params_model=_DescriptionParams,
            ),
        ]
    )

    response = await agent.process_message("Look up deck pricing and generate an estimate")

    # Both tools should have been called
    mock_recall.assert_called_once()
    mock_estimate.assert_called_once()

    # 3 LLM calls total (round 1 + round 2 + final)
    assert mock_amessages.call_count == 3  # type: ignore[union-attr]

    # Final reply comes from the text response
    assert response.reply_text == "Here's your estimate for the deck build!"

    # Both tool calls should be recorded
    assert len(response.tool_calls) == 2
    assert response.tool_calls[0].name == "recall_facts"
    assert response.tool_calls[1].name == "generate_estimate"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_tool_loop_respects_max_rounds(
    mock_amessages: object, test_user: UserData
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

    mock_amessages.side_effect = tool_responses  # type: ignore[union-attr]

    mock_recall = AsyncMock(return_value=ToolResult(content="some result"))
    agent = ClawboltAgent(user=test_user)
    agent.register_tools(
        [
            Tool(
                name="recall_facts",
                description="Recall facts",
                function=mock_recall,
                params_model=_QueryParams,
            ),
        ]
    )

    response = await agent.process_message("Keep going forever")

    # Should have made exactly MAX_TOOL_ROUNDS calls, not more
    assert mock_amessages.call_count == MAX_TOOL_ROUNDS  # type: ignore[union-attr]

    # Should still return a reply (from the last response's content)
    assert response.reply_text == "Still thinking..."


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_handles_malformed_tool_arguments(
    mock_amessages: object, test_user: UserData
) -> None:
    """Agent should gracefully handle None/malformed tool call arguments.

    In the Messages API, tool inputs are always dicts. This test verifies
    the agent handles the edge case where parse_tool_calls returns
    arguments=None (e.g. from a content block with missing input).
    """
    from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage

    # Build a response with a tool_use block that has None input
    tool_response = MessageResponse(
        id="msg_mock",
        content=[
            MessageContentBlock(type="tool_use", id="call_bad", name="save_fact", input=None),
        ],
        model="mock-model",
        stop_reason="tool_use",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )
    followup_response = make_text_response("Sorry, I had trouble with that.")

    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_save = AsyncMock(return_value=ToolResult(content="saved"))
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        params_model=_KeyValueParams,
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $75/hour")

    # Tool should NOT have been called (args were None)
    mock_save.assert_not_called()

    # Agent should still produce a reply (not crash)
    assert response.reply_text == "Sorry, I had trouble with that."

    # The failure should be recorded in actions_taken
    assert any("bad args" in a for a in response.actions_taken)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_passes_dict_arguments_to_tool(
    mock_amessages: object, test_user: UserData
) -> None:
    """Messages API delivers tool inputs as dicts; agent should pass them through."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_dict",
                "name": "save_fact",
                "arguments": {"key": "hourly_rate", "value": "$75/hr"},
            }
        ]
    )
    followup_response = make_text_response("Got it!")

    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_save = AsyncMock(return_value=ToolResult(content="Saved hourly_rate = $75/hr"))
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        params_model=_KeyValueParams,
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $75/hour")

    mock_save.assert_called_once_with(key="hourly_rate", value="$75/hr")
    assert any("Called save_fact" in a for a in response.actions_taken)


# ---------------------------------------------------------------------------
# Typed LLM exception handling tests (issue #173)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.asyncio.sleep", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_agent_retries_on_rate_limit_error(
    mock_amessages: AsyncMock,
    mock_sleep: AsyncMock,
    test_user: UserData,
) -> None:
    """RateLimitError should trigger one retry after a delay."""
    mock_amessages.side_effect = [
        RateLimitError("Too many requests"),
        make_text_response("Retry succeeded!"),
    ]

    agent = ClawboltAgent(user=test_user)
    response = await agent.process_message("Hello")

    assert response.reply_text == "Retry succeeded!"
    assert mock_amessages.call_count == 2
    mock_sleep.assert_called_once()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.asyncio.sleep", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_agent_rate_limit_retry_failure_propagates(
    mock_amessages: AsyncMock,
    mock_sleep: AsyncMock,
    test_user: UserData,
) -> None:
    """If the retry after RateLimitError also fails, the exception propagates."""
    mock_amessages.side_effect = [
        RateLimitError("Too many requests"),
        RateLimitError("Still rate limited"),
    ]

    agent = ClawboltAgent(user=test_user)
    with pytest.raises(RateLimitError):
        await agent.process_message("Hello")

    assert mock_amessages.call_count == 2


# ---------------------------------------------------------------------------
# Context window overflow protection tests (issue #172)
# ---------------------------------------------------------------------------


def test_removed_token_estimation_helpers() -> None:
    """Verify legacy token estimation helpers have been removed."""
    from backend.app.agent import core as agent_core

    assert not hasattr(agent_core, "_log_token_estimation_drift")
    assert not hasattr(agent_core, "_estimate_tokens")
    assert not hasattr(agent_core, "_total_content_length")


def test_trim_messages_preserves_tool_call_result_pairs() -> None:
    """Trimming should never orphan a tool result by removing its tool_call."""
    system = SystemMessage(content="x" * 3500)
    user1 = UserMessage(content="x" * 3500)
    assistant_tc = AssistantMessage(
        content=None,
        tool_calls=[ToolCallRequest(id="call_1", name="save_fact", arguments={})],
    )
    tool_result = ToolResultMessage(tool_call_id="call_1", content="x" * 3500)
    user2 = UserMessage(content="Final question")

    messages = [system, user1, assistant_tc, tool_result, user2]

    # Use a small budget that forces trimming. Simulate prior API response
    # reporting 2625 input tokens for this conversation.
    trimmed = trim_messages(messages, target_tokens=1000, input_tokens=2625)

    # The trimmed result should never contain tool_result without assistant_tc
    has_tool_msg = any(isinstance(m, ToolResultMessage) for m in trimmed)
    has_tc_msg = any(isinstance(m, AssistantMessage) and m.tool_calls for m in trimmed)

    if has_tool_msg:
        assert has_tc_msg, "Tool result present without its tool_call assistant message"
    if has_tc_msg:
        assert has_tool_msg, "Tool call assistant message present without its tool result"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_trims_context_on_context_length_exceeded(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """ContextLengthExceededError should trim messages and retry once."""
    mock_amessages.side_effect = [
        ContextLengthExceededError("Input too long"),
        make_text_response("Trimmed and retried!"),
    ]

    # Supply a long conversation history with large messages to trigger trimming
    big_content = "x" * 4000
    long_history: list[AgentMessage] = [
        UserMessage(content=f"Message {i}: {big_content}")
        if i % 2 == 0
        else AssistantMessage(content=f"Message {i}: {big_content}")
        for i in range(150)
    ]

    agent = ClawboltAgent(user=test_user)
    response = await agent.process_message("Current message", conversation_history=long_history)

    assert response.reply_text == "Trimmed and retried!"
    assert mock_amessages.call_count == 2

    # Verify the retry call preserves the system prompt
    retry_call = mock_amessages.call_args_list[1]
    assert retry_call.kwargs["system"] is not None
    retry_messages = retry_call.kwargs["messages"]
    # Messages should have been trimmed (fewer than original 150)
    assert len(retry_messages) < 150


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_trims_history_when_exceeding_token_limit(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """Messages should be trimmed when provider token counts exceed MAX_INPUT_TOKENS."""
    mock_amessages.return_value = make_text_response("Trimmed reply!")

    # Create a huge conversation history that exceeds MAX_INPUT_TOKENS
    big_content = "x" * 4000
    long_history: list[AgentMessage] = [
        UserMessage(content=big_content) if i % 2 == 0 else AssistantMessage(content=big_content)
        for i in range(150)
    ]

    agent = ClawboltAgent(user=test_user)
    # Simulate a prior LLM response so the pre-call trimmer has token data.
    agent._last_input_tokens = 600_000
    response = await agent.process_message("Current message", conversation_history=long_history)

    assert response.reply_text == "Trimmed reply!"

    # Verify that the messages sent to amessages were trimmed
    call_args = mock_amessages.call_args
    messages = call_args.kwargs["messages"]
    # Original was 150 history + 1 current; should be significantly trimmed
    assert len(messages) < 150


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_raises_content_filter_error(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """ContentFilterError should be re-raised (handled by router)."""
    mock_amessages.side_effect = ContentFilterError("Blocked by safety filter")

    agent = ClawboltAgent(user=test_user)
    with pytest.raises(ContentFilterError):
        await agent.process_message("Something problematic")

    assert mock_amessages.call_count == 1


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_preserves_system_and_user_during_trimming(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """System prompt and latest user message must survive trimming."""
    mock_amessages.return_value = make_text_response("Ok!")

    # Create history that will trigger trimming
    big_content = "x" * 4000
    long_history: list[AgentMessage] = [
        UserMessage(content=big_content) if i % 2 == 0 else AssistantMessage(content=big_content)
        for i in range(150)
    ]

    agent = ClawboltAgent(user=test_user)
    await agent.process_message(
        "My important question",
        conversation_history=long_history,
        system_prompt_override="Custom system prompt",
    )

    call_args = mock_amessages.call_args
    messages = call_args.kwargs["messages"]

    # System prompt is passed as 'system' kwarg
    assert call_args.kwargs["system"] == "Custom system prompt"

    # Latest user message is always last
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "My important question"

    # At least 1 message (system is extracted to kwarg)
    assert len(messages) >= 1


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_raises_authentication_error(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """AuthenticationError should be re-raised (handled by router)."""
    mock_amessages.side_effect = AuthenticationError("Invalid API key")

    agent = ClawboltAgent(user=test_user)
    with pytest.raises(AuthenticationError):
        await agent.process_message("Hello")

    assert mock_amessages.call_count == 1


def test_trim_messages_skips_without_input_tokens() -> None:
    """Without input_tokens, _trim_messages returns messages unchanged."""
    big_content = "x" * 4000
    messages: list[AgentMessage] = [
        SystemMessage(content="System prompt"),
        *[UserMessage(content=big_content) for _ in range(50)],
    ]
    trimmed = trim_messages(messages, target_tokens=100)
    assert trimmed is messages


def test_trim_messages_preserves_short_conversation() -> None:
    """Messages shorter than the threshold should be returned unchanged."""
    messages: list[AgentMessage] = [
        SystemMessage(content="System prompt"),
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there!"),
    ]
    # With a small input_tokens count that fits the budget, no trimming occurs.
    trimmed = trim_messages(messages, input_tokens=50)
    assert trimmed == messages


def test_trim_messages_keeps_system_and_recent() -> None:
    """Long conversations should be trimmed to fit within the token budget."""
    big_content = "x" * 4000
    messages: list[AgentMessage] = [
        SystemMessage(content="System prompt"),
        *[
            UserMessage(content=big_content)
            if i % 2 == 0
            else AssistantMessage(content=big_content)
            for i in range(20)
        ],
    ]
    # Simulate a prior API response reporting 20_000 input tokens.
    trimmed = trim_messages(messages, target_tokens=5000, input_tokens=20_000)
    assert isinstance(trimmed[0], SystemMessage)
    # Should have been trimmed significantly
    assert len(trimmed) < len(messages)
    # Last message should be the most recent one
    assert trimmed[-1] == messages[-1]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_does_not_trim_normal_conversations(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """Normal-sized conversations should not be trimmed."""
    mock_amessages.return_value = make_text_response("Got it!")

    history: list[AgentMessage] = [
        UserMessage(content="Hi, I need help"),
        AssistantMessage(content="Hello! How can I help?"),
        UserMessage(content="Can you estimate a deck?"),
        AssistantMessage(content="Sure, what size?"),
    ]

    agent = ClawboltAgent(user=test_user)
    await agent.process_message(
        "12x12 composite deck",
        conversation_history=history,
        system_prompt_override="System prompt",
    )

    call_args = mock_amessages.call_args
    messages = call_args.kwargs["messages"]

    # 4 history + 1 current = 5 (system is extracted to kwarg)
    assert len(messages) == 5


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_logs_warning_when_trimming(
    mock_amessages: AsyncMock,
    test_user: UserData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning should be logged when conversation history is trimmed."""
    mock_amessages.return_value = make_text_response("Ok!")

    big_content = "x" * 4000
    long_history: list[AgentMessage] = [
        UserMessage(content=big_content) if i % 2 == 0 else AssistantMessage(content=big_content)
        for i in range(150)
    ]

    agent = ClawboltAgent(user=test_user)
    # Simulate a prior LLM response so the pre-call trimmer has token data.
    agent._last_input_tokens = 600_000

    with caplog.at_level("WARNING", logger="backend.app.agent.core"):
        await agent.process_message(
            "Current message",
            conversation_history=long_history,
            system_prompt_override="Short system prompt",
        )

    assert any("Trimmed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Context compaction / summary injection tests
# ---------------------------------------------------------------------------


def test_summarize_dropped_messages_includes_user_topics() -> None:
    """Summary should include first lines from dropped user messages."""
    from backend.app.agent.trimming import summarize_dropped_messages

    dropped: list[AgentMessage] = [
        UserMessage(content="What did I quote for the Johnson deck?"),
        AssistantMessage(content="You quoted $4,500 for the 12x12 composite deck."),
    ]
    summary = summarize_dropped_messages(dropped)
    assert "2 earlier message(s)" in summary
    assert "Johnson deck" in summary


def test_summarize_dropped_messages_includes_tool_calls() -> None:
    """Summary should mention tools that were called in dropped messages."""
    from backend.app.agent.trimming import summarize_dropped_messages

    dropped: list[AgentMessage] = [
        UserMessage(content="Save my rate"),
        AssistantMessage(
            content=None,
            tool_calls=[ToolCallRequest(id="call_1", name="save_fact", arguments={})],
        ),
        ToolResultMessage(tool_call_id="call_1", content="Saved"),
        AssistantMessage(content="Done!"),
    ]
    summary = summarize_dropped_messages(dropped)
    assert "save_fact" in summary
    assert "Tools used:" in summary


def test_summarize_dropped_messages_empty_list() -> None:
    """Empty dropped list should produce a zero-count summary."""
    from backend.app.agent.trimming import summarize_dropped_messages

    summary = summarize_dropped_messages([])
    assert "0 earlier message(s)" in summary


def test_trim_messages_injects_summary_when_trimming() -> None:
    """When _trim_messages drops messages, a summary should be injected."""
    big_content = "x" * 4000
    messages: list[AgentMessage] = [
        SystemMessage(content="System prompt"),
        *[
            UserMessage(content=f"Topic {i}: {big_content}")
            if i % 2 == 0
            else AssistantMessage(content=big_content)
            for i in range(20)
        ],
    ]
    trimmed = trim_messages(messages, target_tokens=5000, input_tokens=20_000)
    assert isinstance(trimmed[0], SystemMessage)
    # Second message should be the summary
    assert isinstance(trimmed[1], UserMessage)
    assert "[Summary of earlier conversation:" in trimmed[1].content
    assert "earlier message(s)" in trimmed[1].content


def test_trim_messages_no_summary_when_not_trimmed() -> None:
    """No summary should be injected when messages fit within the budget."""
    messages: list[AgentMessage] = [
        SystemMessage(content="System prompt"),
        UserMessage(content="Hello"),
        AssistantMessage(content="Hi there!"),
    ]
    trimmed = trim_messages(messages, input_tokens=50)
    assert trimmed == messages
    # No summary message should be present
    for msg in trimmed:
        if isinstance(msg, UserMessage):
            assert "[Summary of earlier conversation:" not in msg.content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_process_message_injects_summary_when_trimming(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """process_message should inject a summary when history is trimmed."""
    mock_amessages.return_value = make_text_response("Ok!")

    big_content = "x" * 4000
    long_history: list[AgentMessage] = [
        UserMessage(content=f"Topic {i}: {big_content}")
        if i % 2 == 0
        else AssistantMessage(content=big_content)
        for i in range(150)
    ]

    agent = ClawboltAgent(user=test_user)
    # Simulate a prior LLM response so the pre-call trimmer has token data.
    agent._last_input_tokens = 600_000

    await agent.process_message(
        "Current message",
        conversation_history=long_history,
        system_prompt_override="Short system prompt",
    )

    # Check the messages sent to the LLM include a summary
    call_args = mock_amessages.call_args
    sent_messages = call_args.kwargs.get("messages", call_args.args[0] if call_args.args else [])
    # First message should be the summary (system is extracted to kwarg)
    assert "[Summary of earlier conversation:" in sent_messages[0]["content"]


# ---------------------------------------------------------------------------
# Dict-based tool registry tests (issue #282)
# ---------------------------------------------------------------------------


def test_register_tools_builds_dict_lookup(test_user: UserData) -> None:
    """register_tools should build a dict for O(1) lookup by name."""
    agent = ClawboltAgent(user=test_user)

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(name="tool_a", description="A", function=dummy, params_model=_EmptyParams),
        Tool(name="tool_b", description="B", function=dummy, params_model=_EmptyParams),
    ]
    agent.register_tools(tools)

    assert agent._find_tool("tool_a") is not None
    assert agent._find_tool("tool_b") is not None
    assert agent._find_tool("nonexistent") is None


def test_register_tools_warns_on_duplicate_name(
    test_user: UserData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Registering tools with duplicate names should log a warning."""
    agent = ClawboltAgent(user=test_user)

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(name="dupe", description="First", function=dummy, params_model=_EmptyParams),
        Tool(name="dupe", description="Second", function=dummy, params_model=_EmptyParams),
    ]

    with caplog.at_level("WARNING", logger="backend.app.agent.core"):
        agent.register_tools(tools)

    assert any("Duplicate tool name" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Structured ToolResult tests (issue #280)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_tool_result_error_appends_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """When a tool returns ToolResult(is_error=True), a hint is appended."""

    async def failing_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Error: item not found", is_error=True)

    tool = Tool(
        name="do_thing", description="test", function=failing_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "do_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("I'll try something else."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # The hint should have been appended to the error result
    assert any("Failed: do_thing" in a for a in response.actions_taken)
    assert response.tool_calls[0].is_error is True
    assert "[Analyze the error" in response.tool_calls[0].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_tool_result_success_no_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """When a tool returns ToolResult(is_error=False), no hint is appended."""

    async def ok_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Done!")

    tool = Tool(name="do_thing", description="test", function=ok_tool, params_model=_EmptyParams)

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "do_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("Great!"),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    assert any("Called do_thing" in a for a in response.actions_taken)
    assert response.tool_calls[0].is_error is False
    assert "[Analyze the error" not in response.tool_calls[0].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_tool_exception_appends_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """When a tool raises an exception, a self-correction hint is appended."""

    async def crashing_tool(**kwargs: object) -> ToolResult:
        raise RuntimeError("Something broke")

    tool = Tool(
        name="bad_tool", description="test", function=crashing_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "bad_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Let me try another way."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    assert any("Failed: bad_tool" in a for a in response.actions_taken)


# ---------------------------------------------------------------------------
# Tool error feedback tests (issue #292)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_unknown_tool_error_lists_available_tools(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """Unknown tool error should list all registered tool names."""

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tools = [
        Tool(
            name="save_fact", description="Save a fact", function=dummy, params_model=_EmptyParams
        ),
        Tool(
            name="recall_facts",
            description="Recall facts",
            function=dummy,
            params_model=_EmptyParams,
        ),
    ]

    # LLM calls a tool that doesn't exist
    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[
                {"id": "call_1", "name": "save_notes", "arguments": json.dumps({"text": "hi"})}
            ]
        ),
        make_text_response("Let me try again."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM (wrapped as user message
    # with tool_result content blocks in Messages API format)
    followup_call = mock_amessages.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    # Find the user message containing tool_result content blocks
    tool_msg = next(
        m
        for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    )
    content = tool_msg["content"][0]["content"]

    assert 'unknown tool "save_notes"' in content
    assert "save_fact" in content
    assert "recall_facts" in content
    assert "[Analyze the error" in content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_validation_error_includes_expected_schema(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """Validation errors should include the expected parameter schema."""
    from pydantic import BaseModel

    class FactParams(BaseModel):
        key: str
        value: str
        category: str = "general"

    async def save_fact(**kwargs: object) -> ToolResult:
        return ToolResult(content="saved")

    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=save_fact,
        params_model=FactParams,
    )

    # LLM calls save_fact with missing required fields
    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[
                {"id": "call_1", "name": "save_fact", "arguments": json.dumps({"category": "job"})}
            ]
        ),
        make_text_response("Let me fix that."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM (wrapped as user message
    # with tool_result content blocks in Messages API format)
    followup_call = mock_amessages.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    tool_msg = next(
        m
        for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    )
    content = tool_msg["content"][0]["content"]

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
@patch("backend.app.agent.core.amessages")
async def test_error_kind_not_found_produces_specific_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """NOT_FOUND error kind should produce a resource-not-found hint."""

    async def missing_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: item #42 not found",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )

    tool = Tool(
        name="find_item", description="test", function=missing_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "find_item", "arguments": json.dumps({})}]
        ),
        make_text_response("That item doesn't exist."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    assert "not found" in result_content.lower()
    assert "[The requested resource was not found" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_kind_service_produces_specific_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """SERVICE error kind should produce an external-service hint."""

    async def failing_service(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: Dropbox API unavailable",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )

    tool = Tool(
        name="upload", description="test", function=failing_service, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "upload", "arguments": json.dumps({})}]
        ),
        make_text_response("Storage is down."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    assert "[An external service is temporarily unavailable" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_kind_validation_produces_specific_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """VALIDATION error kind should produce a parameter-check hint."""

    async def bad_args_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: quantity must be positive",
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )

    tool = Tool(
        name="create_thing", description="test", function=bad_args_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "create_thing", "arguments": json.dumps({})}]
        ),
        make_text_response("Let me fix that."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    assert "[Check the expected parameter format" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_kind_internal_produces_specific_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """INTERNAL error kind should produce a do-not-retry hint."""

    async def buggy_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: unexpected None in config",
            is_error=True,
            error_kind=ToolErrorKind.INTERNAL,
        )

    tool = Tool(
        name="broken_tool", description="test", function=buggy_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "broken_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Something went wrong."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    assert "[An internal error occurred" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_with_no_kind_uses_default_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """ToolResult with is_error=True but no error_kind should use the default hint."""

    async def legacy_error_tool(**kwargs: object) -> ToolResult:
        return ToolResult(content="Error: something went wrong", is_error=True)

    tool = Tool(
        name="legacy_tool",
        description="test",
        function=legacy_error_tool,
        params_model=_EmptyParams,
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "legacy_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("I'll try another way."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    assert "[Analyze the error above and try a different approach.]" in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_with_custom_hint_overrides_kind_default(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """ToolResult with a custom hint should use it instead of the error_kind default."""

    async def custom_hint_tool(**kwargs: object) -> ToolResult:
        return ToolResult(
            content="Error: estimate #99 not found",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
            hint="Ask the user for the correct estimate number.",
        )

    tool = Tool(
        name="get_estimate",
        description="test",
        function=custom_hint_tool,
        params_model=_EmptyParams,
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "get_estimate", "arguments": json.dumps({})}]
        ),
        make_text_response("Which estimate?"),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    result_content = response.tool_calls[0].result
    # Custom hint should appear, not the default NOT_FOUND hint
    assert "[Ask the user for the correct estimate number.]" in result_content
    assert "requested resource was not found" not in result_content


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_different_error_kinds_produce_different_hints(
    mock_amessages: AsyncMock,
    test_user: UserData,
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

        tool = Tool(
            name="test_tool", description="test", function=make_tool, params_model=_EmptyParams
        )

        mock_amessages.reset_mock()
        mock_amessages.side_effect = [
            make_tool_call_response(
                tool_calls=[{"id": "call_1", "name": "test_tool", "arguments": json.dumps({})}]
            ),
            make_text_response("ok"),
        ]

        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("test", system_prompt_override="system")

        collected_hints[kind.value] = response.tool_calls[0].result

    # All hints should be different from each other
    hint_values = list(collected_hints.values())
    assert len(set(hint_values)) == len(hint_values), (
        f"Expected all error kinds to produce unique hints, got: {collected_hints}"
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_unhandled_exception_uses_internal_hint(
    mock_amessages: AsyncMock,
    test_user: UserData,
) -> None:
    """Unhandled tool exceptions should produce INTERNAL error kind hint."""

    async def crashing_tool(**kwargs: object) -> ToolResult:
        raise RuntimeError("Unexpected crash")

    tool = Tool(
        name="crash_tool", description="test", function=crashing_tool, params_model=_EmptyParams
    )

    mock_amessages.side_effect = [
        make_tool_call_response(
            tool_calls=[{"id": "call_1", "name": "crash_tool", "arguments": json.dumps({})}]
        ),
        make_text_response("Something went wrong."),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Check the tool result sent back to the LLM (wrapped as user message
    # with tool_result content blocks in Messages API format)
    followup_call = mock_amessages.call_args_list[1]
    messages = followup_call.kwargs["messages"]
    tool_msg = next(
        m
        for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    )
    content = tool_msg["content"][0]["content"]

    assert "[An internal error occurred" in content


# ---- Tool Registry Tests ----


class TestToolRegistry:
    """Tests for the ToolRegistry and related helpers."""

    def test_register_and_create_tools(self, test_user: UserData) -> None:
        """Registry should create tools from registered factories."""
        from backend.app.agent.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry()

        def dummy_factory(ctx: ToolContext) -> list[Tool]:
            async def noop() -> ToolResult:
                return ToolResult(content="ok")

            return [
                Tool(
                    name="dummy",
                    description="test tool",
                    function=noop,
                    params_model=_EmptyParams,
                )
            ]

        registry.register("dummy", dummy_factory)
        ctx = ToolContext(user=test_user)
        tools = registry.create_tools(ctx)
        assert len(tools) == 1
        assert tools[0].name == "dummy"

    def test_skips_factory_when_storage_missing(self, test_user: UserData) -> None:
        """Factories requiring storage should be skipped when storage is None."""
        from backend.app.agent.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry()

        def storage_factory(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="needs_storage",
                    description="test",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        registry.register("storage_tool", storage_factory, requires_storage=True)
        ctx = ToolContext(user=test_user, storage=None)
        tools = registry.create_tools(ctx)
        assert len(tools) == 0

    def test_skips_factory_when_outbound_missing(self, test_user: UserData) -> None:
        """Factories requiring outbound should be skipped when publish_outbound is None."""
        from backend.app.agent.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry()

        def msg_factory(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="needs_outbound",
                    description="test",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        registry.register("msg_tool", msg_factory, requires_outbound=True)
        ctx = ToolContext(user=test_user, publish_outbound=None)
        tools = registry.create_tools(ctx)
        assert len(tools) == 0

    def test_includes_factory_when_deps_satisfied(
        self,
        test_user: UserData,
    ) -> None:
        """Factories should produce tools when required dependencies are present."""
        from unittest.mock import AsyncMock, MagicMock

        from backend.app.agent.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry()

        def storage_factory(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="with_storage",
                    description="test",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        def msg_factory(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="with_msg",
                    description="test",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        registry.register("s", storage_factory, requires_storage=True)
        registry.register("m", msg_factory, requires_outbound=True)

        mock_storage = MagicMock()
        mock_publish = AsyncMock()
        ctx = ToolContext(
            user=test_user,
            storage=mock_storage,
            publish_outbound=mock_publish,
        )
        tools = registry.create_tools(ctx)
        names = {t.name for t in tools}
        assert "with_storage" in names
        assert "with_msg" in names

    def test_factory_names_sorted(self) -> None:
        """factory_names property should return sorted list of registered names."""
        from backend.app.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("zebra", lambda ctx: [])
        registry.register("alpha", lambda ctx: [])
        registry.register("middle", lambda ctx: [])
        assert registry.factory_names == ["alpha", "middle", "zebra"]

    def test_default_registry_has_all_modules(self) -> None:
        """The default_registry should have all tool modules registered."""
        from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

        ensure_tool_modules_imported()
        names = default_registry.factory_names
        assert "memory" in names
        assert "messaging" in names
        assert "estimate" in names
        assert "checklist" in names
        assert "profile" in names
        assert "file" in names

    def test_ensure_tool_modules_imported_idempotent(self) -> None:
        """Calling ensure_tool_modules_imported() multiple times should be safe."""
        from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

        ensure_tool_modules_imported()
        count1 = len(default_registry.factory_names)
        ensure_tool_modules_imported()
        count2 = len(default_registry.factory_names)
        assert count1 == count2

    def test_overwrite_warns(self, test_user: UserData) -> None:
        """Registering the same name twice should overwrite (with a warning)."""
        from backend.app.agent.tools.registry import ToolContext, ToolRegistry

        registry = ToolRegistry()

        def factory_a(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="a",
                    description="first",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        def factory_b(ctx: ToolContext) -> list[Tool]:
            return [
                Tool(
                    name="b",
                    description="second",
                    function=lambda: None,
                    params_model=_EmptyParams,
                )
            ]

        registry.register("same_name", factory_a)
        registry.register("same_name", factory_b)

        ctx = ToolContext(user=test_user)
        tools = registry.create_tools(ctx)
        # The second factory should win
        assert len(tools) == 1
        assert tools[0].name == "b"


# ---------------------------------------------------------------------------
# Debug logging tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_emits_debug_logs_for_full_loop(
    mock_amessages: AsyncMock,
    test_user: UserData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Debug logging should trace the full agent loop lifecycle."""
    tool_func = AsyncMock(return_value=ToolResult(content="saved"))
    tool = Tool(
        name="save_fact",
        description="save",
        function=tool_func,
        params_model=_KeyValueParams,
    )
    mock_amessages.side_effect = [
        make_tool_call_response(
            [{"name": "save_fact", "arguments": {"key": "color", "value": "blue"}}]
        ),
        make_text_response("Done!"),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])

    with caplog.at_level("DEBUG", logger="backend.app.agent.core"):
        await agent.process_message(
            "Remember my favorite color is blue",
            system_prompt_override="You are a helpful assistant.",
        )

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    # Agent start
    assert any("Agent starting" in m for m in debug_messages)
    # Round logging
    assert any("Round 0" in m and "starting" in m for m in debug_messages)
    # Tool calls parsed
    assert any("save_fact" in m and "tool call" in m for m in debug_messages)
    # Tool execution result
    assert any("save_fact" in m and "completed" in m for m in debug_messages)
    # Agent finished
    assert any("Agent finished" in m for m in debug_messages)
    # LLM call
    assert any("Calling LLM" in m for m in debug_messages)


# ---------------------------------------------------------------------------
# Error stop_reason guard (context poisoning prevention)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_stop_reason_sets_is_error_fallback(
    mock_amessages: object, test_user: UserData
) -> None:
    """LLM response with an error stop_reason should set is_error_fallback."""
    mock_amessages.return_value = make_error_response(stop_reason="error")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    response = await agent.process_message("Hello")

    assert response.is_error_fallback is True
    assert response.reply_text  # should have a fallback message


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_error_stop_reason_mid_loop_preserves_earlier_tool_calls(
    mock_amessages: object, test_user: UserData
) -> None:
    """Error stop_reason after a successful tool round should preserve tool records."""
    tool_func = AsyncMock(return_value=ToolResult(content="saved"))
    tool = Tool(
        name="save_fact",
        description="save",
        function=tool_func,
        params_model=_KeyValueParams,
    )
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(
            [{"name": "save_fact", "arguments": {"key": "color", "value": "blue"}}]
        ),
        # Second LLM call returns an error
        make_error_response(stop_reason="error"),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("Remember blue")

    assert response.is_error_fallback is True
    # The successful tool call from round 0 should still be in the records
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "save_fact"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_tool_errors_still_returned_to_llm_in_loop(
    mock_amessages: object, test_user: UserData
) -> None:
    """Tool-level errors should still be fed back to the LLM for self-correction."""
    tool_func = AsyncMock(return_value=ToolResult(content="saved"))
    tool = Tool(
        name="save_fact",
        description="save",
        function=tool_func,
        params_model=_KeyValueParams,
    )
    # Round 0: LLM calls an unknown tool (error result returned)
    # Round 1: LLM self-corrects and calls the right tool
    # Round 2: LLM produces final text reply
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(
            [{"name": "nonexistent_tool", "arguments": {"key": "a", "value": "b"}}]
        ),
        make_tool_call_response(
            [{"name": "save_fact", "arguments": {"key": "color", "value": "blue"}}]
        ),
        make_text_response("Done!"),
    ]

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("Remember blue")

    # The agent should NOT be flagged as error (the loop recovered)
    assert response.is_error_fallback is False
    assert response.reply_text == "Done!"
    # The LLM was called 3 times (error feedback allowed self-correction)
    assert mock_amessages.call_count == 3  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_valid_stop_reasons_not_treated_as_error(
    mock_amessages: object, test_user: UserData
) -> None:
    """Responses with valid stop_reasons should be processed normally."""
    for stop_reason in ("end_turn", "max_tokens", "stop_sequence"):
        from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage

        mock_amessages.return_value = MessageResponse(  # type: ignore[union-attr]
            id="msg_mock",
            content=[MessageContentBlock(type="text", text="Reply!")],
            model="mock-model",
            stop_reason=stop_reason,
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )

        agent = ClawboltAgent(user=test_user)
        response = await agent.process_message("Hello")

        assert response.is_error_fallback is False, f"stop_reason={stop_reason} was wrongly error"
        assert response.reply_text == "Reply!"
