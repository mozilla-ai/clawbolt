"""Tests for profile tools: view_profile, update_profile, and helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.profile_tools import (
    _format_profile,
    create_profile_tools,
    extract_profile_updates_from_tool_calls,
)

# --- Helper to get tool functions by name ---


def _get_tool_fn(
    contractor: ContractorData, tool_name: str
) -> Callable[..., Awaitable[ToolResult]]:
    """Return the async function for the named tool."""
    tools = create_profile_tools(contractor)
    for t in tools:
        if t.name == tool_name:
            return t.function
    msg = f"Tool {tool_name!r} not found"
    raise ValueError(msg)


# --- view_profile tool tests ---


@pytest.mark.asyncio()
async def test_view_profile_shows_populated_fields(
    test_contractor: ContractorData,
) -> None:
    """view_profile should return all populated profile fields."""
    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert result.is_error is False
    assert "Test Contractor" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_not_set_for_empty_fields(
    test_contractor: ContractorData,
) -> None:
    """view_profile should show 'Not set' for empty name."""
    store = get_contractor_store()
    await store.update(test_contractor.id, name="")

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert result.is_error is False
    assert "Name: Not set" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_onboarding_status(
    test_contractor: ContractorData,
) -> None:
    """view_profile should show onboarding completion status."""
    store = get_contractor_store()
    await store.update(test_contractor.id, onboarding_complete=True)

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "Onboarding Complete: Yes" in result.content


@pytest.mark.asyncio()
async def test_view_profile_reflects_updates(
    test_contractor: ContractorData,
) -> None:
    """view_profile should reflect changes made by update_profile."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    await update_fn(name="Jake the Plumber")

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "Jake the Plumber" in result.content


@pytest.mark.asyncio()
async def test_view_profile_mentions_user_md(
    test_contractor: ContractorData,
) -> None:
    """view_profile should mention USER.md for additional details."""
    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "USER.md" in result.content


# --- _format_profile unit tests ---


def test_format_profile_complete(test_contractor: ContractorData) -> None:
    """_format_profile should include core fields for a populated profile."""
    contractor = ContractorData(
        id=test_contractor.id,
        user_id=test_contractor.user_id,
        name="Test Contractor",
        assistant_name="Bolt",
    )

    output = _format_profile(contractor)
    assert "Test Contractor" in output
    assert "Bolt" in output


def test_format_profile_empty_contractor() -> None:
    """_format_profile should show 'Not set' for all fields on a blank contractor."""
    contractor = ContractorData(user_id="blank-user")

    output = _format_profile(contractor)
    assert "Name: Not set" in output
    assert "AI Name: Clawbolt (default)" in output


# --- update_profile tool unit tests ---


@pytest.mark.asyncio()
async def test_update_profile_name(test_contractor: ContractorData) -> None:
    """update_profile should update contractor name."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(name="Mike Johnson")
    assert "name" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.name == "Mike Johnson"


@pytest.mark.asyncio()
async def test_update_profile_assistant_name(test_contractor: ContractorData) -> None:
    """update_profile should update assistant name."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(assistant_name="Bolt")
    assert "assistant_name" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.assistant_name == "Bolt"


@pytest.mark.asyncio()
async def test_update_profile_multiple_fields(
    test_contractor: ContractorData,
) -> None:
    """update_profile should update multiple fields at once."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(name="Jake", assistant_name="Bolt")
    assert result.is_error is False
    assert "name" in result.content
    assert "assistant_name" in result.content
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.name == "Jake"
    assert refreshed.assistant_name == "Bolt"


@pytest.mark.asyncio()
async def test_update_profile_no_fields(test_contractor: ContractorData) -> None:
    """update_profile should return error when no fields provided."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn()
    assert result.is_error is True
    assert "No fields provided" in result.content


# --- extract_profile_updates_from_tool_calls tests ---


def test_extract_from_update_profile_calls() -> None:
    """Should extract profile fields from update_profile tool call records."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"name": "Mike"},
            result="Profile updated: name",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Mike"


def test_extract_ignores_non_update_profile_tools() -> None:
    """Should ignore tool calls that are not update_profile."""
    tool_calls = [
        StoredToolInteraction(
            name="save_fact",
            args={"key": "name", "value": "Mike"},
            result="Saved: name = Mike",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates == {}


def test_extract_ignores_error_tool_calls() -> None:
    """Should ignore update_profile calls that had errors."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"name": ""},
            result="No fields provided to update.",
            is_error=True,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates == {}


def test_extract_multiple_update_profile_calls() -> None:
    """Should merge results from multiple update_profile calls."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"name": "Jake"},
            result="Profile updated: name",
            is_error=False,
        ),
        StoredToolInteraction(
            name="update_profile",
            args={"assistant_name": "Bolt"},
            result="Profile updated: assistant_name",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Jake"
    assert updates["assistant_name"] == "Bolt"


def test_extract_all_fields() -> None:
    """Should extract all supported profile fields."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={
                "name": "Sarah",
                "assistant_name": "Sparky",
            },
            result="Profile updated: all fields",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Sarah"
    assert updates["assistant_name"] == "Sparky"


# --- Tool schema tests ---


def test_tool_list_contains_both_tools(test_contractor: ContractorData) -> None:
    """create_profile_tools should return both view_profile and update_profile."""
    tools = create_profile_tools(test_contractor)
    names = [t.name for t in tools]
    assert "view_profile" in names
    assert "update_profile" in names
    assert len(tools) == 2


def test_view_profile_tool_schema(test_contractor: ContractorData) -> None:
    """view_profile tool should have correct name and no required parameters."""
    tools = create_profile_tools(test_contractor)
    tool = next(t for t in tools if t.name == "view_profile")
    assert tool.params_model is not None
    schema = tool.params_model.model_json_schema()
    assert schema["properties"] == {}


def test_update_profile_tool_schema(test_contractor: ContractorData) -> None:
    """update_profile tool should have correct name and params_model schema."""
    tools = create_profile_tools(test_contractor)
    tool = next(t for t in tools if t.name == "update_profile")
    assert tool.name == "update_profile"
    assert tool.params_model is not None
    schema = tool.params_model.model_json_schema()
    props = schema["properties"]
    assert "name" in props
    assert "assistant_name" in props
    # These fields now live in USER.md
    assert "trade" not in props
    assert "location" not in props
    assert "hourly_rate" not in props
    assert "business_hours" not in props
    assert "timezone" not in props
    assert "communication_style" not in props
    assert "soul_text" not in props
    # No required fields since all are optional
    assert "required" not in schema
