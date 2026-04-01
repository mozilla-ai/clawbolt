"""Tool validation error formatting and LLM error hints.

Provides structured error messages for tool argument validation failures
and hint text that guides the LLM toward corrective action.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from backend.app.agent.tools.base import (
    Tool,
    ToolErrorKind,
    ToolResult,
    _inline_refs,
)

_DEFAULT_ERROR_HINT = "[Analyze the error above and try a different approach.]"

_TRUNCATION_HINT = (
    "[Your previous response was cut short (hit the token limit) and the tool call"
    " was incomplete. Simplify your approach: use smaller payloads, break the"
    " operation into multiple steps, or omit optional fields.]"
)

_ERROR_KIND_HINTS: dict[ToolErrorKind, str] = {
    ToolErrorKind.VALIDATION: (
        "[Check the expected parameter format and try again with corrected arguments.]"
    ),
    ToolErrorKind.NOT_FOUND: (
        "[The requested resource was not found. Verify the identifier and try again.]"
    ),
    ToolErrorKind.SERVICE: (
        "[An external service is temporarily unavailable."
        " Try a different approach or inform the user.]"
    ),
    ToolErrorKind.PERMISSION: ("[You do not have permission for this operation. Inform the user.]"),
    ToolErrorKind.AUTH: (
        "[This integration is not connected. Do not retry."
        " Let the user know they need to authenticate via the web dashboard.]"
    ),
    ToolErrorKind.INTERNAL: (
        "[An internal error occurred."
        " Inform the user that this operation is temporarily unavailable.]"
    ),
    ToolErrorKind.INTERRUPTED: (
        "[The user changed the subject instead of responding to the approval prompt."
        " Do not retry this tool. Briefly acknowledge you are setting this aside"
        " (one short sentence, no details) and end your turn."
        " The user's new message is being handled separately.]"
    ),
}


def build_error_hint(result: ToolResult) -> str:
    """Build the LLM guidance suffix for an error ToolResult.

    Priority: explicit ``hint`` on the result, then ``error_kind`` mapping,
    then the generic default.
    """
    if result.hint:
        return f"[{result.hint}]" if not result.hint.startswith("[") else result.hint
    if result.error_kind is not None:
        return _ERROR_KIND_HINTS.get(result.error_kind, _DEFAULT_ERROR_HINT)
    return _DEFAULT_ERROR_HINT


def format_validation_error(tool_name: str, exc: ValidationError, tool: Tool | None = None) -> str:
    """Format a Pydantic ValidationError into a structured message for the LLM."""
    error_lines: list[str] = [f"Validation error for {tool_name}:"]
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err["loc"])
        error_lines.append(f"  {loc}: {err['msg']} (type={err['type']})")

    if tool is not None:
        schema_summary = summarize_tool_params(tool)
        if schema_summary:
            error_lines.append(f"\nExpected parameters: {schema_summary}")

    return "\n".join(error_lines)


def _extract_type_label(info: dict[str, Any]) -> str:
    """Extract a human-readable type label from a JSON Schema property."""
    if "type" in info:
        ptype = info["type"]
        if ptype == "array" and "items" in info:
            items = info["items"]
            if items.get("type") == "object" and "properties" in items:
                item_parts = _summarize_properties(
                    items["properties"], set(items.get("required", []))
                )
                return "array of {" + ", ".join(item_parts) + "}"
            return f"array of {_extract_type_label(items)}"
        return ptype
    if "anyOf" in info:
        types = [alt.get("type", "any") for alt in info["anyOf"] if alt.get("type") != "null"]
        return types[0] if types else "any"
    return "any"


def _summarize_properties(props: dict[str, Any], required: set[str]) -> list[str]:
    """Summarize a set of JSON Schema properties into label strings."""
    parts: list[str] = []
    for name, info in props.items():
        ptype = _extract_type_label(info)
        req = "required" if name in required else "optional"
        default = info.get("default")
        if default is not None:
            parts.append(f'"{name}": {ptype} ({req}, default: {default})')
        else:
            parts.append(f'"{name}": {ptype} ({req})')
    return parts


def summarize_tool_params(tool: Tool) -> str:
    """Build a concise parameter summary string from a tool's schema."""
    schema = tool.params_model.model_json_schema()
    schema = _inline_refs(schema)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return ""

    parts = _summarize_properties(props, required)
    return "{" + ", ".join(parts) + "}"
