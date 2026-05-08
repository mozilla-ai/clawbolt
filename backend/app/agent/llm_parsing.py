"""Shared LLM response parsing utilities.

Centralizes tool call extraction from ``MessageResponse`` responses so that
both the main agent loop and the heartbeat engine share the same parsing
and validation logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from any_llm.types.messages import MessageResponse, TextBlock, ThinkingBlock, ToolUseBlock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedToolCall:
    """A single tool call extracted from an LLM response.

    ``arguments`` is ``None`` when the input could not be validated
    as a dict (unexpected type, etc.).
    """

    id: str
    name: str
    arguments: dict[str, Any] | None


def parse_tool_calls(response: MessageResponse) -> list[ParsedToolCall]:
    """Extract tool calls from a ``MessageResponse``.

    Returns an empty list when the LLM returned plain text (no tool use
    blocks).  Each ``tool_use`` content block is converted to a
    ``ParsedToolCall`` with its ``input`` dict as ``arguments``.
    """
    result: list[ParsedToolCall] = []
    for block in response.content:
        if not isinstance(block, ToolUseBlock):
            continue

        block_id = block.id or ""
        block_name = block.name or ""
        arguments = block.input

        result.append(
            ParsedToolCall(
                id=block_id,
                name=block_name,
                arguments=arguments,
            )
        )

    if result:
        logger.debug(
            "Parsed %d tool call(s) from LLM response: %s",
            len(result),
            ", ".join(
                f"{tc.name}({', '.join(tc.arguments.keys()) if tc.arguments else ''})"
                for tc in result
            ),
        )
    return result


def get_response_text(response: MessageResponse) -> str:
    """Extract the text content from a ``MessageResponse``.

    Concatenates all ``text`` content blocks.  Returns an empty string
    when there is no text content.
    """
    parts: list[str] = []
    for block in response.content:
        if isinstance(block, TextBlock) and block.text:
            parts.append(block.text)
    return "".join(parts)


def get_response_thinking(response: MessageResponse) -> str:
    """Extract the extended-thinking text from a ``MessageResponse``.

    Concatenates the ``thinking`` field across every ``ThinkingBlock``
    in ``response.content``, separated by blank lines so multi-block
    streams remain readable when rendered. Returns an empty string when
    no thinking blocks are present (the model didn't think, or thinking
    wasn't enabled in the request). Empty thinking blocks are skipped.

    The cryptographic ``signature`` field on each block is not surfaced;
    it is only meaningful for replaying the block back to Anthropic and
    has no audit value to a human reader.
    """
    parts: list[str] = []
    for block in response.content:
        if isinstance(block, ThinkingBlock) and block.thinking:
            parts.append(block.thinking)
    return "\n\n".join(parts)
