from typing import Any

from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage


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

    blocks: list[MessageContentBlock] = []

    if content:
        blocks.append(MessageContentBlock(type="text", text=content))

    for i, tc in enumerate(tool_calls):
        args = tc["arguments"]
        if isinstance(args, str):
            args = json.loads(args)
        if not isinstance(args, dict):
            args = {}
        blocks.append(
            MessageContentBlock(
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
        stop_reason="tool_use",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )


def _make_text_message_response(content: str) -> MessageResponse:
    """Build a mock MessageResponse with a single text block."""
    return MessageResponse(
        id="msg_mock",
        content=[MessageContentBlock(type="text", text=content)],
        model="mock-model",
        stop_reason="end_turn",
        usage=MessageUsage(input_tokens=0, output_tokens=0),
    )
