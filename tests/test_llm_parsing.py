"""Tests for shared LLM response parsing utilities."""

import json

from any_llm.types.messages import MessageResponse, MessageUsage, TextBlock, ToolUseBlock

from backend.app.agent.llm_parsing import ParsedToolCall, get_response_text, parse_tool_calls
from tests.mocks.llm import make_text_response, make_tool_call_response


def _make_response(blocks: list[TextBlock | ToolUseBlock]) -> MessageResponse:
    """Helper to build a MessageResponse from content blocks.

    Uses ``model_construct`` to bypass pydantic validation so tests can
    pass arbitrary content block lists.
    """
    return MessageResponse.model_construct(
        id="msg_test",
        content=blocks,
        model="test-model",
        role="assistant",
        type="message",
        stop_reason="end_turn",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )


class TestParseToolCalls:
    def test_valid_single_tool_call(self) -> None:
        """A well-formed tool call should be parsed correctly."""
        resp = make_tool_call_response(
            [{"name": "save_fact", "arguments": json.dumps({"key": "name", "value": "Mike"})}]
        )
        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].name == "save_fact"
        assert result[0].arguments == {"key": "name", "value": "Mike"}
        assert result[0].id == "call_0"

    def test_valid_multiple_tool_calls(self) -> None:
        """Multiple tool calls should all be parsed."""
        resp = make_tool_call_response(
            [
                {"name": "tool_a", "arguments": json.dumps({"x": 1})},
                {"name": "tool_b", "arguments": json.dumps({"y": 2}), "id": "custom_id"},
            ]
        )
        result = parse_tool_calls(resp)
        assert len(result) == 2
        assert result[0].name == "tool_a"
        assert result[0].arguments == {"x": 1}
        assert result[1].name == "tool_b"
        assert result[1].arguments == {"y": 2}
        assert result[1].id == "custom_id"

    def test_no_tool_calls_returns_empty(self) -> None:
        """A text response (no tool calls) should return an empty list."""
        resp = make_text_response("Hello there")
        result = parse_tool_calls(resp)
        assert result == []

    def test_text_blocks_are_skipped(self) -> None:
        """Text content blocks should not appear in tool call results."""
        resp = _make_response(
            [
                TextBlock(type="text", text="thinking..."),
                ToolUseBlock(type="tool_use", id="call_0", name="save_fact", input={"key": "v"}),
            ]
        )
        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].name == "save_fact"

    def test_empty_content_returns_empty(self) -> None:
        """Empty content list should return empty result."""
        resp = _make_response([])
        result = parse_tool_calls(resp)
        assert result == []

    def test_parsed_tool_call_is_frozen(self) -> None:
        """ParsedToolCall should be immutable."""
        ptc = ParsedToolCall(id="1", name="test", arguments={"a": 1})
        try:
            ptc.name = "changed"  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


class TestGetResponseText:
    def test_returns_content(self) -> None:
        """Should return the text content of the response."""
        resp = make_text_response("Hello world")
        assert get_response_text(resp) == "Hello world"

    def test_returns_empty_for_no_text_blocks(self) -> None:
        """Should return empty string when there are no text blocks."""
        resp = _make_response(
            [
                ToolUseBlock(type="tool_use", id="call_0", name="some_tool", input={"a": 1}),
            ]
        )
        assert get_response_text(resp) == ""

    def test_concatenates_multiple_text_blocks(self) -> None:
        """Multiple text blocks should be concatenated."""
        resp = _make_response(
            [
                TextBlock(type="text", text="Hello "),
                TextBlock(type="text", text="world"),
            ]
        )
        assert get_response_text(resp) == "Hello world"

    def test_returns_empty_for_empty_content(self) -> None:
        """Should return empty string when content list is empty."""
        resp = _make_response([])
        assert get_response_text(resp) == ""
