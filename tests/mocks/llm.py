from typing import Any
from unittest.mock import MagicMock

from any_llm.types.messages import MessageResponse, MessageUsage, TextBlock, ToolUseBlock


def extract_system_text(system: str | list[dict[str, Any]] | None) -> str:
    """Extract plain text from a system prompt that may use cache content blocks.

    After enabling prompt caching, the ``system`` kwarg passed to
    ``amessages()`` is a list of content blocks rather than a plain string.
    This helper normalizes both formats to a single string for test assertions.
    """
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "".join(block.get("text", "") for block in system)


def make_vision_response(
    description: str = "A 12x12 composite deck with cedar railing, showing minor weathering.",
) -> MessageResponse:
    """Build a mock any-llm MessageResponse for vision calls."""
    return _make_text_message_response(description)


def make_text_response(content: str = "I'll help you with that.") -> MessageResponse:
    """Build a mock any-llm MessageResponse for text calls."""
    return _make_text_message_response(content)


def make_tool_call_response(
    tool_calls: list[dict[str, Any]],
    content: str | None = None,
) -> MessageResponse:
    """Build a mock MessageResponse with tool_use content blocks.

    Each tool_call dict should have: name, arguments (JSON string or dict),
    and optionally id.
    """
    import json

    blocks: list[Any] = []

    if content:
        blocks.append(TextBlock(type="text", text=content))

    for i, tc in enumerate(tool_calls):
        args = tc["arguments"]
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(args, dict):
            args = {}
        blocks.append(
            ToolUseBlock(
                type="tool_use",
                id=tc.get("id", f"call_{i}"),
                name=tc["name"],
                input=args,
            )
        )

    return MessageResponse(
        id="msg_mock",
        content=blocks,
        model="mock-model",
        role="assistant",
        type="message",
        stop_reason="tool_use",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )


def make_truncated_tool_call_response(
    tool_calls: list[dict[str, Any]],
    content: str | None = None,
) -> MessageResponse:
    """Build a mock MessageResponse with tool_use blocks and stop_reason='max_tokens'.

    Simulates an LLM response that was truncated mid-tool-call due to
    hitting the max_tokens limit.
    """
    resp = make_tool_call_response(tool_calls, content)
    return MessageResponse(
        id=resp.id,
        content=resp.content,
        model=resp.model,
        role="assistant",
        type="message",
        stop_reason="max_tokens",
        usage=resp.usage,
    )


def make_error_response(
    stop_reason: str = "error",
    content: str = "",
) -> MessageResponse:
    """Build a mock MessageResponse with an error stop_reason.

    Simulates an LLM response that completed but with an error status,
    such as ``stop_reason="error"`` from certain providers.

    Uses MagicMock because MessageResponse in any-llm 1.13+ validates
    stop_reason as a literal enum and rejects arbitrary values like "error".
    """
    blocks: list[Any] = []
    if content:
        blocks.append(TextBlock(type="text", text=content))
    mock = MagicMock(spec=MessageResponse)
    mock.id = "msg_mock"
    mock.content = blocks
    mock.model = "mock-model"
    mock.role = "assistant"
    mock.type = "message"
    mock.stop_reason = stop_reason
    mock.usage = MessageUsage(input_tokens=0, output_tokens=0)
    return mock


def make_empty_response() -> MessageResponse:
    """Build a mock MessageResponse with no content blocks (empty reply)."""
    return MessageResponse(
        id="msg_mock",
        content=[],
        model="mock-model",
        role="assistant",
        type="message",
        stop_reason="end_turn",
        usage=MessageUsage(input_tokens=0, output_tokens=2),
    )


def _make_text_message_response(content: str) -> MessageResponse:
    """Build a mock MessageResponse with a single text block."""
    return MessageResponse(
        id="msg_mock",
        content=[TextBlock(type="text", text=content)],
        model="mock-model",
        role="assistant",
        type="message",
        stop_reason="end_turn",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )
