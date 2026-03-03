"""Typed message dataclasses for the agent loop.

These replace raw ``dict[str, Any]`` messages inside the agent, providing
type safety and eliminating fragile ``.get()`` chains.  Dict serialization
happens only at the LLM API boundary via ``to_dict()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCallRequest:
    """A single tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class SystemMessage:
    """System prompt message."""

    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": "system", "content": self.content}


@dataclass(frozen=True)
class UserMessage:
    """User (contractor) message."""

    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


@dataclass(frozen=True)
class AssistantMessage:
    """Assistant response, optionally containing tool calls."""

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": _serialize_arguments(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        return d


@dataclass(frozen=True)
class ToolResultMessage:
    """Result of a tool execution, sent back to the LLM."""

    tool_call_id: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


# Union of all message types the agent loop works with.
AgentMessage = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage


def messages_to_dicts(messages: list[AgentMessage]) -> list[dict[str, Any]]:
    """Serialize a list of typed messages to LLM-compatible dicts.

    The return type is compatible with the ``messages`` parameter of
    ``acompletion`` (which accepts ``list[dict[str, Any] | ChatCompletionMessage]``).
    """
    result: list[dict[str, Any]] = [m.to_dict() for m in messages]
    return result


def _serialize_arguments(arguments: dict[str, Any]) -> str:
    """Serialize tool call arguments to a JSON string for the LLM API."""
    import json

    return json.dumps(arguments)
