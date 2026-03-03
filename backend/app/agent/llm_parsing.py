"""Shared LLM response parsing utilities.

Centralizes tool call extraction from ``ChatCompletion`` responses so that
both the main agent loop and the heartbeat engine share the same parsing,
JSON repair, and validation logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import json_repair
from any_llm.types.completion import ChatCompletion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedToolCall:
    """A single tool call extracted from an LLM response.

    ``arguments`` is ``None`` when the raw arguments could not be parsed
    into a dict (malformed JSON, non-dict result, etc.).
    """

    id: str
    name: str
    arguments: dict[str, Any] | None


def parse_tool_calls(response: ChatCompletion) -> list[ParsedToolCall]:
    """Extract tool calls from a ``ChatCompletion`` response.

    Returns an empty list when the LLM returned plain text (no tool calls).
    Each element has its ``arguments`` parsed via ``json_repair`` and
    validated as a ``dict``.  When parsing fails, ``arguments`` is ``None``
    so callers can decide how to handle the error.
    """
    choice = response.choices[0]
    raw_tool_calls = getattr(choice.message, "tool_calls", None)

    if not raw_tool_calls:
        return []

    result: list[ParsedToolCall] = []
    for raw_tc in raw_tool_calls:
        func = getattr(raw_tc, "function", None)
        if func is None:
            continue

        arguments = _parse_arguments(func.arguments)

        result.append(
            ParsedToolCall(
                id=raw_tc.id,
                name=func.name,
                arguments=arguments,
            )
        )

    return result


def get_response_text(response: ChatCompletion) -> str:
    """Extract the text content from a ``ChatCompletion`` response.

    Returns an empty string when there is no content.
    """
    return response.choices[0].message.content or ""


def _parse_arguments(raw_arguments: str | None) -> dict[str, Any] | None:
    """Parse raw JSON arguments string into a dict.

    Returns ``None`` when the input is missing, empty, or cannot be parsed
    into a dict.
    """
    if not raw_arguments:
        return None

    try:
        parsed = json_repair.loads(raw_arguments)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (ValueError, TypeError):
        return None
