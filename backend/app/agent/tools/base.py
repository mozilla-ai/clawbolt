from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


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
    parameters: dict[str, Any] = field(default_factory=dict)
    params_model: type[BaseModel] | None = None


def tool_to_openai_schema(tool: Tool) -> dict[str, Any]:
    """Convert a Tool to OpenAI function calling schema.

    When a params_model is set, the JSON Schema is generated from the Pydantic
    model (single source of truth). Otherwise falls back to the raw dict.
    """
    if tool.params_model is not None:
        schema = tool.params_model.model_json_schema()
        # Pydantic v2 includes a top-level "title" key that OpenAI does not
        # expect. Remove it to keep the schema clean.
        schema.pop("title", None)
        parameters = schema
    else:
        parameters = tool.parameters

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }
