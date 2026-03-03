from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


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
