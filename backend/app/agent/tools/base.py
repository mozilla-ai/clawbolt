from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolTags:
    """Constants for cross-cutting tool metadata tags."""

    SENDS_REPLY = "sends_reply"
    SAVES_MEMORY = "saves_memory"


@dataclass
class ToolResult:
    """Structured result from a tool execution."""

    content: str
    is_error: bool = False


@dataclass
class Tool:
    """A tool that the agent can call."""

    name: str
    description: str
    function: Callable[..., Any]
    parameters: dict[str, Any]
    tags: set[str] = field(default_factory=set)


def tool_to_openai_schema(tool: Tool) -> dict[str, Any]:
    """Convert a Tool to OpenAI function calling schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
