from unittest.mock import MagicMock


def make_vision_response(
    description: str = "A 12x12 composite deck with cedar railing, showing minor weathering.",
) -> MagicMock:
    """Build a mock any-llm response for vision calls."""
    return _make_completion_response(description)


def make_text_response(content: str = "I'll help you with that.") -> MagicMock:
    """Build a mock any-llm response for text calls."""
    return _make_completion_response(content)


def make_tool_call_response(
    tool_calls: list[dict[str, str]],
    content: str | None = None,
) -> MagicMock:
    """Build a mock ChatCompletion response with tool_calls.

    Each tool_call dict should have: name, arguments (JSON string), and optionally id.
    """
    mock_tool_calls = []
    for i, tc in enumerate(tool_calls):
        mock_tc = MagicMock()
        mock_tc.id = tc.get("id", f"call_{i}")
        mock_tc.function.name = tc["name"]
        mock_tc.function.arguments = tc["arguments"]
        mock_tool_calls.append(mock_tc)

    msg = MagicMock()
    msg.content = content
    msg.tool_calls = mock_tool_calls
    msg.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in mock_tool_calls
        ],
    }

    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def _make_completion_response(content: str) -> MagicMock:
    """Build a mock ChatCompletion response (no tool calls)."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None  # Explicitly no tool calls
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp
