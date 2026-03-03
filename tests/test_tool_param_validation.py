"""Tests for Pydantic validation of tool parameters (issue #277)."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.tools.base import Tool, ToolResult, tool_to_openai_schema
from backend.app.agent.tools.checklist_tools import (
    AddChecklistItemParams,
    ListChecklistItemsParams,
    RemoveChecklistItemParams,
)
from backend.app.agent.tools.estimate_tools import (
    EstimateLineItemParams,
    GenerateEstimateParams,
)
from backend.app.agent.tools.file_tools import OrganizeFileParams, UploadToStorageParams
from backend.app.agent.tools.memory_tools import (
    ForgetFactParams,
    RecallFactsParams,
    SaveFactParams,
)
from backend.app.agent.tools.messaging_tools import SendMediaReplyParams, SendReplyParams
from backend.app.models import Contractor
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Schema generation tests: params_model produces correct OpenAI schema
# ---------------------------------------------------------------------------


class SampleParams(BaseModel):
    """Sample param model for testing schema generation."""

    name: str = Field(description="A name")
    count: int = Field(default=1, description="A count")


def test_tool_to_openai_schema_uses_params_model() -> None:
    """When params_model is set, schema should be auto-generated from the model."""

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tool = Tool(
        name="sample",
        description="A sample tool",
        function=dummy,
        params_model=SampleParams,
    )
    schema = tool_to_openai_schema(tool)
    params = schema["function"]["parameters"]

    # Should have properties from the Pydantic model
    assert "name" in params["properties"]
    assert "count" in params["properties"]
    # Title should be stripped
    assert "title" not in params
    # Required fields: only 'name' (count has a default)
    assert "name" in params["required"]
    assert "count" not in params.get("required", [])


def test_tool_to_openai_schema_falls_back_to_raw_dict() -> None:
    """When no params_model is set, raw parameters dict should be used."""

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    raw_params = {"type": "object", "properties": {"x": {"type": "string"}}}
    tool = Tool(
        name="sample",
        description="A sample tool",
        function=dummy,
        parameters=raw_params,
    )
    schema = tool_to_openai_schema(tool)
    assert schema["function"]["parameters"] is raw_params


# ---------------------------------------------------------------------------
# Param model existence tests: every tool module defines models
# ---------------------------------------------------------------------------


def test_memory_tool_param_models_exist() -> None:
    """Memory tool param models should be importable and valid BaseModels."""
    assert issubclass(SaveFactParams, BaseModel)
    assert issubclass(RecallFactsParams, BaseModel)
    assert issubclass(ForgetFactParams, BaseModel)


def test_messaging_tool_param_models_exist() -> None:
    """Messaging tool param models should be importable and valid BaseModels."""
    assert issubclass(SendReplyParams, BaseModel)
    assert issubclass(SendMediaReplyParams, BaseModel)


def test_estimate_tool_param_models_exist() -> None:
    """Estimate tool param models should be importable and valid BaseModels."""
    assert issubclass(GenerateEstimateParams, BaseModel)


def test_checklist_tool_param_models_exist() -> None:
    """Checklist tool param models should be importable and valid BaseModels."""
    assert issubclass(AddChecklistItemParams, BaseModel)
    assert issubclass(ListChecklistItemsParams, BaseModel)
    assert issubclass(RemoveChecklistItemParams, BaseModel)


def test_file_tool_param_models_exist() -> None:
    """File tool param models should be importable and valid BaseModels."""
    assert issubclass(UploadToStorageParams, BaseModel)
    assert issubclass(OrganizeFileParams, BaseModel)


# ---------------------------------------------------------------------------
# Validation behavior tests: type coercion and error handling
# ---------------------------------------------------------------------------


def test_save_fact_params_accepts_valid_input() -> None:
    """SaveFactParams should accept valid arguments."""
    p = SaveFactParams(key="rate", value="$50/hr", category="pricing")
    assert p.key == "rate"
    assert p.value == "$50/hr"
    assert p.category == "pricing"


def test_save_fact_params_default_category() -> None:
    """SaveFactParams should default category to 'general'."""
    p = SaveFactParams(key="note", value="test")
    assert p.category == "general"


def test_save_fact_params_rejects_invalid_category() -> None:
    """SaveFactParams should reject invalid category values."""
    with pytest.raises(Exception):  # noqa: B017
        SaveFactParams(key="note", value="test", category="invalid")  # type: ignore[invalid-argument-type]


def test_remove_checklist_item_coerces_string_to_int() -> None:
    """RemoveChecklistItemParams should coerce string '42' to int 42."""
    p = RemoveChecklistItemParams(item_id="42")  # type: ignore[arg-type]
    assert p.item_id == 42


def test_remove_checklist_item_rejects_non_numeric() -> None:
    """RemoveChecklistItemParams should reject non-numeric item_id."""
    with pytest.raises(Exception):  # noqa: B017
        RemoveChecklistItemParams(item_id="not_a_number")  # type: ignore[arg-type]


def test_generate_estimate_params_accepts_valid_input() -> None:
    """GenerateEstimateParams should accept valid arguments with line items."""
    p = GenerateEstimateParams(
        description="Deck work",
        line_items=[EstimateLineItemParams(description="Materials", unit_price=100.0, quantity=2)],
    )
    assert p.description == "Deck work"
    assert len(p.line_items) == 1
    assert p.line_items[0].unit_price == 100.0


def test_generate_estimate_params_rejects_missing_line_items() -> None:
    """GenerateEstimateParams should reject missing line_items."""
    with pytest.raises(Exception):  # noqa: B017
        GenerateEstimateParams(description="Deck work")  # type: ignore[call-arg]


def test_upload_to_storage_rejects_invalid_category() -> None:
    """UploadToStorageParams should reject invalid file_category."""
    with pytest.raises(Exception):  # noqa: B017
        UploadToStorageParams(file_category="invalid_category")  # type: ignore[arg-type]


def test_add_checklist_item_rejects_invalid_schedule() -> None:
    """AddChecklistItemParams should reject invalid schedule values."""
    with pytest.raises(Exception):  # noqa: B017
        AddChecklistItemParams(description="test", schedule="monthly")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Agent-level validation integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_validation_failure_returns_error_result(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When params_model validation fails, agent should return a structured error."""
    # LLM sends wrong type for 'key' (int instead of string) to save_fact
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_1",
                "name": "typed_tool",
                "arguments": json.dumps({"value": 42}),  # missing required 'name'
            }
        ]
    )
    followup_response = make_text_response("Let me fix that.")
    mock_acompletion.side_effect = [tool_response, followup_response]

    class TypedParams(BaseModel):
        name: str = Field(description="A required name")
        value: int = Field(description="A value")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="typed_tool",
        description="A typed tool",
        function=mock_func,
        params_model=TypedParams,
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Tool function should NOT have been called
    mock_func.assert_not_called()

    # The error should be recorded
    assert any("Failed: typed_tool (validation)" in a for a in response.actions_taken)
    assert response.tool_calls[0]["is_error"] is True
    assert "Validation error" in response.tool_calls[0]["result"]
    assert "name" in response.tool_calls[0]["result"]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_validation_success_calls_tool(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When params_model validation passes, agent should call the tool normally."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_1",
                "name": "typed_tool",
                "arguments": json.dumps({"name": "test", "value": 42}),
            }
        ]
    )
    followup_response = make_text_response("Done!")
    mock_acompletion.side_effect = [tool_response, followup_response]

    class TypedParams(BaseModel):
        name: str = Field(description="A required name")
        value: int = Field(description="A value")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="typed_tool",
        description="A typed tool",
        function=mock_func,
        params_model=TypedParams,
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Tool function should have been called with validated args
    mock_func.assert_called_once_with(name="test", value=42)
    assert any("Called typed_tool" in a for a in response.actions_taken)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_validation_coerces_types(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Pydantic validation should coerce compatible types (e.g., str '42' to int)."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_1",
                "name": "typed_tool",
                "arguments": json.dumps({"name": "test", "value": "42"}),
            }
        ]
    )
    followup_response = make_text_response("Done!")
    mock_acompletion.side_effect = [tool_response, followup_response]

    class TypedParams(BaseModel):
        name: str = Field(description="A required name")
        value: int = Field(description="A value")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="typed_tool",
        description="A typed tool",
        function=mock_func,
        params_model=TypedParams,
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Should be called with coerced int, not string
    mock_func.assert_called_once_with(name="test", value=42)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_validation_wrong_type_returns_field_error(
    mock_acompletion: AsyncMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Validation error message should include the specific field that failed."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_1",
                "name": "typed_tool",
                "arguments": json.dumps({"name": "ok", "value": "not_a_number"}),
            }
        ]
    )
    followup_response = make_text_response("Let me fix that.")
    mock_acompletion.side_effect = [tool_response, followup_response]

    class TypedParams(BaseModel):
        name: str = Field(description="A name")
        value: int = Field(description="Must be an integer")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="typed_tool",
        description="A typed tool",
        function=mock_func,
        params_model=TypedParams,
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    mock_func.assert_not_called()
    error_result = response.tool_calls[0]["result"]
    assert "value" in error_result
    assert "Validation error for typed_tool" in error_result
