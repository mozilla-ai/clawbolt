"""Tests for typed agent message dataclasses (issue #311)."""

import json

from backend.app.agent.messages import (
    AssistantMessage,
    SystemMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
    messages_to_dicts,
)


def test_system_message_to_dict() -> None:
    msg = SystemMessage(content="You are helpful.")
    d = msg.to_dict()
    assert d == {"role": "system", "content": "You are helpful."}


def test_user_message_to_dict() -> None:
    msg = UserMessage(content="Hello!")
    d = msg.to_dict()
    assert d == {"role": "user", "content": "Hello!"}


def test_assistant_message_text_only_to_dict() -> None:
    msg = AssistantMessage(content="Sure, I can help.")
    d = msg.to_dict()
    assert d == {"role": "assistant", "content": "Sure, I can help."}
    assert "tool_calls" not in d


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
    assert d["content"] is None
    assert len(d["tool_calls"]) == 1

    tc = d["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "save_fact"
    args = json.loads(tc["function"]["arguments"])
    assert args == {"key": "rate", "value": "$75/hr"}


def test_tool_result_message_to_dict() -> None:
    msg = ToolResultMessage(tool_call_id="call_1", content="Saved successfully.")
    d = msg.to_dict()
    assert d == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Saved successfully.",
    }


def test_messages_to_dicts_roundtrip() -> None:
    """messages_to_dicts should serialize a conversation to LLM-compatible dicts."""
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
    dicts = messages_to_dicts(messages)
    assert len(dicts) == 5
    assert dicts[0]["role"] == "system"
    assert dicts[1]["role"] == "user"
    assert dicts[2]["role"] == "assistant"
    assert "tool_calls" in dicts[2]
    assert dicts[3]["role"] == "tool"
    assert dicts[4]["role"] == "assistant"
    assert "tool_calls" not in dicts[4]


def test_messages_to_dicts_empty() -> None:
    assert messages_to_dicts([]) == []


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
