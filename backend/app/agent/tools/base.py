from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from backend.app.agent.approval import ApprovalPolicy


class ToolTags(StrEnum):
    """Cross-cutting tool metadata tags."""

    SENDS_REPLY = "sends_reply"
    MODIFIES_PROFILE = "modifies_profile"


class ToolErrorKind(StrEnum):
    """Classification of tool errors to guide LLM self-correction."""

    VALIDATION = "validation"
    SERVICE = "service"
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    AUTH = "auth"
    INTERNAL = "internal"
    INTERRUPTED = "interrupted"


@dataclass
class ToolResult:
    """Structured result from a tool execution."""

    content: str
    is_error: bool = False
    error_kind: ToolErrorKind | None = None
    hint: str = ""


@dataclass
class Tool:
    """A tool that the agent can call."""

    name: str
    description: str
    function: Callable[..., Awaitable[ToolResult]]
    params_model: type[BaseModel]
    tags: set[ToolTags] = field(default_factory=set)
    usage_hint: str = ""
    approval_policy: ApprovalPolicy | None = None


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $defs/$ref references so the schema is self-contained."""
    defs = schema.pop("$defs", {})
    if not defs:
        return schema

    def resolve(obj: Any) -> Any:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                return resolve(defs.get(ref_name, obj))
            return {k: resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [resolve(item) for item in obj]
        return obj

    return resolve(schema)


def _strip_titles(obj: Any) -> Any:
    """Recursively remove all 'title' keys from a schema dict."""
    if isinstance(obj, dict):
        return {k: _strip_titles(v) for k, v in obj.items() if k != "title"}
    if isinstance(obj, list):
        return [_strip_titles(item) for item in obj]
    return obj


def tool_to_function_schema(tool: Tool) -> dict[str, Any]:
    """Convert a Tool to the Anthropic Messages API tool schema.

    The JSON Schema is generated from the tool's ``params_model``
    (Pydantic BaseModel), which is the single source of truth for
    parameter definitions.
    """
    schema = tool.params_model.model_json_schema()
    schema = _inline_refs(schema)
    schema = _strip_titles(schema)

    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": schema,
    }
