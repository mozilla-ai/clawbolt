"""Tests for typed agent message dataclasses and Messages API serialization."""

from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
    messages_to_messages_api,
)


def test_user_message_to_dict() -> None:
    msg = UserMessage(content="Hello!")
    d = msg.to_dict()
    assert d == {"role": "user", "content": "Hello!"}


def test_assistant_message_text_only_to_dict() -> None:
    msg = AssistantMessage(content="Sure, I can help.")
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert d["content"] == [{"type": "text", "text": "Sure, I can help."}]


def test_assistant_message_with_tool_calls_to_dict() -> None:
    msg = AssistantMessage(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="call_1",
                name="save_fact",
                arguments={"key": "rate", "value": "$75/hr"},
            )
        ],
    )
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert len(d["content"]) == 1

    tc = d["content"][0]
    assert tc["type"] == "tool_use"
    assert tc["id"] == "call_1"
    assert tc["name"] == "save_fact"
    assert tc["input"] == {"key": "rate", "value": "$75/hr"}


def test_assistant_message_text_and_tool_calls() -> None:
    msg = AssistantMessage(
        content="Let me check.",
        tool_calls=[ToolCallRequest(id="call_1", name="recall", arguments={"q": "test"})],
    )
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert len(d["content"]) == 2
    assert d["content"][0] == {"type": "text", "text": "Let me check."}
    assert d["content"][1]["type"] == "tool_use"


def test_tool_result_message_to_content_block() -> None:
    msg = ToolResultMessage(tool_call_id="call_1", content="Saved successfully.")
    block = msg.to_content_block()
    assert block == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "Saved successfully.",
    }
    assert "is_error" not in block


def test_tool_result_message_error_to_content_block() -> None:
    msg = ToolResultMessage(tool_call_id="call_1", content="Validation error", is_error=True)
    block = msg.to_content_block()
    assert block == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "Validation error",
        "is_error": True,
    }


def test_messages_to_messages_api_extracts_system() -> None:
    """messages_to_messages_api should extract system prompt and serialize messages."""
    messages = [
        SystemMessage(content="System prompt"),
        UserMessage(content="Hi"),
        AssistantMessage(
            content=None,
            tool_calls=[ToolCallRequest(id="call_1", name="recall", arguments={"q": "test"})],
        ),
        ToolResultMessage(tool_call_id="call_1", content="Found it"),
        AssistantMessage(content="Here is the result."),
    ]
    system, dicts = messages_to_messages_api(messages)
    assert system == "System prompt"
    # 4 logical messages (system is extracted):
    # user, assistant(tool_use), user(tool_result), assistant(text)
    assert len(dicts) == 4
    assert dicts[0]["role"] == "user"
    assert dicts[1]["role"] == "assistant"
    assert dicts[1]["content"][0]["type"] == "tool_use"
    # Tool result is wrapped as a user message
    assert dicts[2]["role"] == "user"
    assert dicts[2]["content"][0]["type"] == "tool_result"
    assert dicts[3]["role"] == "assistant"


def test_messages_to_messages_api_merges_consecutive_tool_results() -> None:
    """Consecutive ToolResultMessages should be merged into one user message."""
    messages: list[AgentMessage] = [
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallRequest(id="call_1", name="tool_a", arguments={}),
                ToolCallRequest(id="call_2", name="tool_b", arguments={}),
            ],
        ),
        ToolResultMessage(tool_call_id="call_1", content="Result A"),
        ToolResultMessage(tool_call_id="call_2", content="Result B"),
    ]
    _, dicts = messages_to_messages_api(messages)
    # assistant + user(2 tool_results)
    assert len(dicts) == 2
    assert dicts[1]["role"] == "user"
    assert len(dicts[1]["content"]) == 2
    assert dicts[1]["content"][0]["tool_use_id"] == "call_1"
    assert dicts[1]["content"][1]["tool_use_id"] == "call_2"


def test_messages_to_messages_api_empty() -> None:
    _, dicts = messages_to_messages_api([])
    assert dicts == []


def test_frozen_dataclasses_are_hashable() -> None:
    """Frozen dataclasses should be usable in sets and as dict keys."""
    msg1 = UserMessage(content="Hi")
    msg2 = UserMessage(content="Hi")
    assert msg1 == msg2
    assert hash(msg1) == hash(msg2)

    s = SystemMessage(content="prompt")
    tr = ToolResultMessage(tool_call_id="c1", content="ok")
    # Should be hashable without errors
    result = {msg1, s, tr}
    assert len(result) == 3


def test_tool_call_request_fields() -> None:
    tc = ToolCallRequest(id="call_x", name="my_tool", arguments={"a": 1, "b": "two"})
    assert tc.id == "call_x"
    assert tc.name == "my_tool"
    assert tc.arguments == {"a": 1, "b": "two"}
