"""Tests for Pydantic validation of tool parameters (issue #277)."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, Field

from backend.app.agent.core import ClawboltAgent, _summarize_tool_params
from backend.app.agent.file_store import ContractorData
from backend.app.agent.tools.base import Tool, ToolResult, tool_to_function_schema
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
from backend.app.agent.tools.profile_tools import UpdateProfileParams
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Schema generation tests: params_model produces correct OpenAI schema
# ---------------------------------------------------------------------------


class SampleParams(BaseModel):
    """Sample param model for testing schema generation."""

    name: str = Field(description="A name")
    count: int = Field(default=1, description="A count")


def test_tool_to_function_schema_uses_params_model() -> None:
    """When params_model is set, schema should be auto-generated from the model."""

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tool = Tool(
        name="sample",
        description="A sample tool",
        function=dummy,
        params_model=SampleParams,
    )
    schema = tool_to_function_schema(tool)
    params = schema["input_schema"]

    # Should have properties from the Pydantic model
    assert "name" in params["properties"]
    assert "count" in params["properties"]
    # Title should be stripped
    assert "title" not in params
    # Required fields: only 'name' (count has a default)
    assert "name" in params["required"]
    assert "count" not in params.get("required", [])


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


def test_profile_tool_param_models_exist() -> None:
    """Profile tool param models should be importable and valid BaseModels."""
    assert issubclass(UpdateProfileParams, BaseModel)


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
@patch("backend.app.agent.core.amessages")
async def test_agent_validation_failure_returns_error_result(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
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
    mock_amessages.side_effect = [tool_response, followup_response]

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

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Tool function should NOT have been called
    mock_func.assert_not_called()

    # The error should be recorded
    assert any("Failed: typed_tool (validation)" in a for a in response.actions_taken)
    assert response.tool_calls[0].is_error is True
    assert "Validation error" in response.tool_calls[0].result
    assert "name" in response.tool_calls[0].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_validation_success_calls_tool(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
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
    mock_amessages.side_effect = [tool_response, followup_response]

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

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Tool function should have been called with validated args
    mock_func.assert_called_once_with(name="test", value=42)
    assert any("Called typed_tool" in a for a in response.actions_taken)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_validation_coerces_types(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
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
    mock_amessages.side_effect = [tool_response, followup_response]

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

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    await agent.process_message("test", system_prompt_override="system")

    # Should be called with coerced int, not string
    mock_func.assert_called_once_with(name="test", value=42)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_validation_wrong_type_returns_field_error(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
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
    mock_amessages.side_effect = [tool_response, followup_response]

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

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    mock_func.assert_not_called()
    error_result = response.tool_calls[0].result
    assert "value" in error_result
    assert "Validation error for typed_tool" in error_result


def test_update_profile_params_accepts_partial_update() -> None:
    """UpdateProfileParams should accept partial updates with all fields optional."""
    p = UpdateProfileParams(name="Jane Doe")
    assert p.name == "Jane Doe"
    assert p.trade is None
    assert p.location is None


def test_update_profile_params_accepts_all_fields() -> None:
    """UpdateProfileParams should accept all fields together."""
    p = UpdateProfileParams(
        name="Jane Doe",
        trade="electrician",
        location="Portland, OR",
        hourly_rate="$85/hr",
        business_hours="Mon-Fri 7am-5pm",
        communication_style="casual",
        soul_text="Friendly and efficient",
    )
    assert p.name == "Jane Doe"
    assert p.hourly_rate == "$85/hr"


# ---------------------------------------------------------------------------
# Eager (batch) validation tests: all errors reported in one round (#350)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_batch_validation_reports_all_errors_at_once(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
) -> None:
    """When multiple tool calls have invalid args, ALL errors should be returned in one round.

    Before eager validation, errors were discovered one at a time: if the first
    tool call failed validation, the LLM would only see that error. Now all
    validation runs upfront, so the LLM sees every error in a single round.
    """

    class StrictParams(BaseModel):
        name: str = Field(description="A required name")
        value: int = Field(description="Must be an integer")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="strict_tool",
        description="A strict tool",
        function=mock_func,
        params_model=StrictParams,
    )

    # Two tool calls in one response, both with invalid args
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_a",
                "name": "strict_tool",
                "arguments": json.dumps({"value": 42}),  # missing required 'name'
            },
            {
                "id": "call_b",
                "name": "strict_tool",
                "arguments": json.dumps({"name": "ok", "value": "not_a_number"}),
            },
        ]
    )
    followup_response = make_text_response("I see both errors. Let me fix them.")
    mock_amessages.side_effect = [tool_response, followup_response]

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Neither call should have executed
    mock_func.assert_not_called()

    # Both validation failures should be recorded
    validation_failures = [a for a in response.actions_taken if "(validation)" in a]
    assert len(validation_failures) == 2

    # Both errors should appear in tool_calls records
    error_records = [tc for tc in response.tool_calls if tc.is_error]
    assert len(error_records) == 2
    assert error_records[0].tool_call_id == "call_a"
    assert error_records[1].tool_call_id == "call_b"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_batch_validation_executes_valid_calls_alongside_invalid(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
) -> None:
    """Valid tool calls should still execute even when other calls in the same batch fail."""

    class StrictParams(BaseModel):
        name: str = Field(description="A required name")
        value: int = Field(description="Must be an integer")

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="strict_tool",
        description="A strict tool",
        function=mock_func,
        params_model=StrictParams,
    )

    # Three calls: first invalid, second valid, third invalid
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_bad1",
                "name": "strict_tool",
                "arguments": json.dumps({"value": 42}),  # missing 'name'
            },
            {
                "id": "call_good",
                "name": "strict_tool",
                "arguments": json.dumps({"name": "test", "value": 7}),
            },
            {
                "id": "call_bad2",
                "name": "strict_tool",
                "arguments": json.dumps({"name": "ok", "value": "NaN"}),
            },
        ]
    )
    followup_response = make_text_response("Done!")
    mock_amessages.side_effect = [tool_response, followup_response]

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    # Only the valid call should have executed
    mock_func.assert_called_once_with(name="test", value=7)

    # Two validation failures + one success
    assert sum(1 for a in response.actions_taken if "(validation)" in a) == 2
    assert sum(1 for a in response.actions_taken if a == "Called strict_tool") == 1

    # Verify tool_call_records: 2 errors + 1 success = 3 records
    assert len(response.tool_calls) == 3
    error_ids = {tc.tool_call_id for tc in response.tool_calls if tc.is_error}
    success_ids = {tc.tool_call_id for tc in response.tool_calls if not tc.is_error}
    assert error_ids == {"call_bad1", "call_bad2"}
    assert success_ids == {"call_good"}


# ---------------------------------------------------------------------------
# Validation error summary tests: nested types in error messages (#434)
# ---------------------------------------------------------------------------


def _make_tool(name: str, params_model: type[BaseModel]) -> Tool:
    """Helper to build a Tool with a dummy function for summary tests."""

    async def dummy(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    return Tool(name=name, description="test", function=dummy, params_model=params_model)


def test_summarize_tool_params_includes_array_item_structure() -> None:
    """Validation error summary should describe array item fields, not just 'array'.

    Regression test for #434: when the LLM omits line_items, the error
    message must show the expected item structure so it can self-correct.
    """
    tool = _make_tool("generate_estimate", GenerateEstimateParams)
    summary = _summarize_tool_params(tool)

    # Should show nested item fields, not bare 'array'
    assert "array of {" in summary
    assert '"description": string' in summary
    assert '"quantity": number' in summary
    assert '"unit_price": number' in summary


def test_summarize_tool_params_resolves_anyof_types() -> None:
    """Optional union fields (str | None) should show the concrete type, not 'any'."""
    tool = _make_tool("generate_estimate", GenerateEstimateParams)
    summary = _summarize_tool_params(tool)

    assert '"client_name": string (optional)' in summary


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_validation_error_for_missing_line_items_shows_item_structure(
    mock_amessages: AsyncMock,
    test_contractor: ContractorData,
) -> None:
    """When line_items is missing, the error sent to the LLM should describe the item schema.

    Regression test for #434: the LLM needs to know what each line item
    looks like in order to construct a valid retry.
    """
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "id": "call_est",
                "name": "estimate_tool",
                "arguments": json.dumps({"description": "Deck repair"}),
            }
        ]
    )
    followup_response = make_text_response("Let me add line items.")
    mock_amessages.side_effect = [tool_response, followup_response]

    mock_func = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="estimate_tool",
        description="Generate an estimate",
        function=mock_func,
        params_model=GenerateEstimateParams,
    )

    agent = ClawboltAgent(contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("test", system_prompt_override="system")

    mock_func.assert_not_called()
    error_result = response.tool_calls[0].result

    # Error should mention the missing field
    assert "line_items" in error_result
    # Error should include the item structure so the LLM can self-correct
    assert "unit_price" in error_result
    assert "array of {" in error_result
