"""Tests for tool_to_function_schema with Pydantic model-based parameters."""

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from backend.app.agent.tools.base import (
    Tool,
    ToolResult,
    _inline_refs,
    _strip_titles,
    tool_to_function_schema,
)
from backend.app.agent.tools.estimate_tools import (
    EstimateLineItemParams,
    GenerateEstimateParams,
)

# --- _inline_refs ---


def test_inline_refs_no_defs() -> None:
    """Schema without $defs should be returned unchanged."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    result = _inline_refs(schema)
    assert result == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


def test_inline_refs_resolves_ref() -> None:
    """$ref entries should be replaced with the actual definition."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "item": {"$ref": "#/$defs/Item"},
        },
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        },
    }
    result = _inline_refs(schema)
    assert "$defs" not in result
    assert "$ref" not in json.dumps(result)
    assert result["properties"]["item"] == {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }


def test_inline_refs_resolves_nested_array_ref() -> None:
    """$ref inside array items should also be inlined."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/LineItem"},
            },
        },
        "$defs": {
            "LineItem": {
                "type": "object",
                "properties": {
                    "desc": {"type": "string"},
                    "qty": {"type": "number"},
                },
            },
        },
    }
    result = _inline_refs(schema)
    assert "$defs" not in result
    assert "$ref" not in json.dumps(result)
    assert result["properties"]["items"]["items"]["properties"]["desc"] == {
        "type": "string",
    }


# --- _strip_titles ---


def test_strip_titles_removes_all_title_keys() -> None:
    """All title keys at any nesting level should be removed."""
    schema: dict[str, Any] = {
        "title": "TopLevel",
        "type": "object",
        "properties": {
            "name": {"title": "Name", "type": "string"},
            "items": {
                "title": "Items",
                "type": "array",
                "items": {
                    "title": "Item",
                    "type": "object",
                    "properties": {
                        "desc": {"title": "Desc", "type": "string"},
                    },
                },
            },
        },
    }
    result = _strip_titles(schema)
    assert "title" not in json.dumps(result)
    assert result["type"] == "object"
    assert result["properties"]["name"] == {"type": "string"}


def test_strip_titles_preserves_non_title_keys() -> None:
    """Non-title keys should remain intact."""
    schema: dict[str, Any] = {
        "title": "Foo",
        "type": "object",
        "description": "A foo object",
    }
    result = _strip_titles(schema)
    assert result == {"type": "object", "description": "A foo object"}


# --- tool_to_function_schema with nested Pydantic model ---


async def _dummy_func() -> ToolResult:
    return ToolResult(content="ok")


def test_tool_to_function_schema_flat_for_estimate_params() -> None:
    """Schema for GenerateEstimateParams (nested model) should have no $defs/$ref."""
    tool = Tool(
        name="generate_estimate",
        description="Generate an estimate",
        function=_dummy_func,
        params_model=GenerateEstimateParams,
    )
    schema = tool_to_function_schema(tool)
    schema_json = json.dumps(schema)

    assert "$defs" not in schema_json
    assert "$ref" not in schema_json

    # Verify the nested line_items structure is properly inlined
    params = schema["function"]["parameters"]
    items_prop = params["properties"]["line_items"]
    assert items_prop["type"] == "array"
    # The items should be an inlined object, not a $ref
    item_schema = items_prop["items"]
    assert "type" in item_schema
    assert item_schema["type"] == "object"
    assert "description" in item_schema["properties"]
    assert "quantity" in item_schema["properties"]
    assert "unit_price" in item_schema["properties"]


def test_tool_to_function_schema_no_titles() -> None:
    """Schema should not contain any 'title' keys at any level."""
    tool = Tool(
        name="generate_estimate",
        description="Generate an estimate",
        function=_dummy_func,
        params_model=GenerateEstimateParams,
    )
    schema = tool_to_function_schema(tool)
    schema_json = json.dumps(schema)
    assert '"title"' not in schema_json


def test_tool_to_function_schema_simple_model() -> None:
    """Simple model (no nesting) should also produce clean schema."""

    class SimpleParams(BaseModel):
        """Simple parameters."""

        name: str = Field(description="A name")
        count: int = Field(default=1, description="A count")

    tool = Tool(
        name="simple",
        description="A simple tool",
        function=_dummy_func,
        params_model=SimpleParams,
    )
    schema = tool_to_function_schema(tool)
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "name" in params["properties"]
    assert "count" in params["properties"]
    assert "$defs" not in json.dumps(schema)
    assert '"title"' not in json.dumps(schema)


def test_tool_to_function_schema_fallback_to_raw_parameters() -> None:
    """When no params_model, should use raw parameters dict as-is."""
    raw_params: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    tool = Tool(
        name="raw",
        description="A raw tool",
        function=_dummy_func,
        parameters=raw_params,
    )
    schema = tool_to_function_schema(tool)
    assert schema["function"]["parameters"] == raw_params


def test_estimate_line_item_params_ge_constraints() -> None:
    """EstimateLineItemParams should enforce ge=0 on quantity and unit_price."""
    # Valid values
    item = EstimateLineItemParams(description="Test", quantity=1, unit_price=50)
    assert item.quantity == 1
    assert item.unit_price == 50

    # Zero should be valid
    item_zero = EstimateLineItemParams(description="Test", quantity=0, unit_price=0)
    assert item_zero.quantity == 0
    assert item_zero.unit_price == 0

    # Negative quantity should fail
    try:
        EstimateLineItemParams(description="Test", quantity=-1, unit_price=50)
        msg = "Expected ValidationError for negative quantity"
        raise AssertionError(msg)
    except ValidationError:
        pass

    # Negative unit_price should fail
    try:
        EstimateLineItemParams(description="Test", quantity=1, unit_price=-10)
        msg = "Expected ValidationError for negative unit_price"
        raise AssertionError(msg)
    except ValidationError:
        pass
