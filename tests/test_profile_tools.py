"""Tests for profile tools: view_profile, update_profile, and helpers."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.profile_tools import (
    _format_profile,
    _parse_rate,
    create_profile_tools,
    extract_profile_updates_from_tool_calls,
)

# --- _parse_rate unit tests ---


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [
        ("$85/hr", 85.0),
        ("$85/hour", 85.0),
        ("$85 per hour", 85.0),
        ("$85 an hour", 85.0),
        ("85 dollars", 85.0),
        ("$85.50", 85.5),
        ("$85.50/hr", 85.5),
        ("85", 85.0),
        ("85.00", 85.0),
        ("$50-75/hr", 50.0),
        ("$4500 per project", 4500.0),
        ("$4,500 per project", 4500.0),
        ("Usually around $80", 80.0),
        ("$125/hour for electrical", 125.0),
        ("  $65 /hr  ", 65.0),
    ],
)
def test_parse_rate_valid_formats(input_value: str, expected: float) -> None:
    """_parse_rate should extract numeric rate from various natural-language formats."""
    assert _parse_rate(input_value) == expected


@pytest.mark.parametrize(
    "input_value",
    [
        "not sure",
        "varies",
        "depends on the job",
        "TBD",
        "",
    ],
)
def test_parse_rate_invalid_returns_none(input_value: str) -> None:
    """_parse_rate should return None for non-numeric values."""
    assert _parse_rate(input_value) is None


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
    assert "General Contractor" in result.content
    assert "Portland, OR" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_not_set_for_empty_fields(
    test_contractor: ContractorData,
) -> None:
    """view_profile should show 'Not set' for empty fields."""
    # Clear some fields via the store
    store = get_contractor_store()
    await store.update(test_contractor.id, hourly_rate=None, business_hours="", soul_text="")

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert result.is_error is False
    assert "Hourly Rate: Not set" in result.content
    assert "Business Hours: Not set" in result.content
    assert "Soul/Bio: Not set" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_rate_formatted(
    test_contractor: ContractorData,
) -> None:
    """view_profile should format hourly rate with dollar sign."""
    store = get_contractor_store()
    await store.update(test_contractor.id, hourly_rate=85.0)

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "$85/hr" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_communication_style(
    test_contractor: ContractorData,
) -> None:
    """view_profile should extract and display communication style from preferences."""
    store = get_contractor_store()
    await store.update(
        test_contractor.id,
        preferences_json=json.dumps({"communication_style": "casual and brief"}),
    )

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "casual and brief" in result.content


@pytest.mark.asyncio()
async def test_view_profile_shows_soul_text(
    test_contractor: ContractorData,
) -> None:
    """view_profile should display soul text."""
    store = get_contractor_store()
    await store.update(test_contractor.id, soul_text="I keep it real with my clients.")

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "I keep it real with my clients." in result.content


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
    await update_fn(name="Jake the Plumber", trade="Plumber")

    view_fn = _get_tool_fn(test_contractor, "view_profile")
    result = await view_fn()
    assert "Jake the Plumber" in result.content
    assert "Plumber" in result.content


# --- _format_profile unit tests ---


def test_format_profile_complete(test_contractor: ContractorData) -> None:
    """_format_profile should include all fields for a fully populated profile."""
    contractor = ContractorData(
        id=test_contractor.id,
        user_id=test_contractor.user_id,
        name="Test Contractor",
        trade="General Contractor",
        location="Portland, OR",
        hourly_rate=100.0,
        business_hours="Mon-Fri 8am-5pm",
        soul_text="Deck specialist",
        preferences_json=json.dumps({"communication_style": "formal"}),
    )

    output = _format_profile(contractor)
    assert "Test Contractor" in output
    assert "General Contractor" in output
    assert "Portland, OR" in output
    assert "$100/hr" in output
    assert "Mon-Fri 8am-5pm" in output
    assert "Deck specialist" in output
    assert "formal" in output


def test_format_profile_empty_contractor() -> None:
    """_format_profile should show 'Not set' for all fields on a blank contractor."""
    contractor = ContractorData(user_id="blank-user")

    output = _format_profile(contractor)
    assert "Name: Not set" in output
    assert "Trade: Not set" in output
    assert "Location: Not set" in output
    assert "Hourly Rate: Not set" in output
    assert "Business Hours: Not set" in output
    assert "Communication Style: Not set" in output
    assert "Soul/Bio: Not set" in output


def test_format_profile_invalid_preferences_json(
    test_contractor: ContractorData,
) -> None:
    """_format_profile should handle malformed preferences_json gracefully."""
    contractor = ContractorData(
        id=test_contractor.id,
        user_id=test_contractor.user_id,
        name=test_contractor.name,
        preferences_json="not valid json",
    )

    output = _format_profile(contractor)
    assert "Communication Style: Not set" in output


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
async def test_update_profile_trade(test_contractor: ContractorData) -> None:
    """update_profile should update contractor trade."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(trade="Electrician")
    assert "trade" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.trade == "Electrician"


@pytest.mark.asyncio()
async def test_update_profile_location(test_contractor: ContractorData) -> None:
    """update_profile should update contractor location."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(location="Denver, CO")
    assert "location" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.location == "Denver, CO"


@pytest.mark.asyncio()
async def test_update_profile_hourly_rate(test_contractor: ContractorData) -> None:
    """update_profile should parse and update hourly rate."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(hourly_rate="$85/hr")
    assert "hourly_rate" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.hourly_rate == 85.0


@pytest.mark.asyncio()
async def test_update_profile_hourly_rate_numeric(
    test_contractor: ContractorData,
) -> None:
    """update_profile should handle numeric hourly rate values."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(hourly_rate=95.0)
    assert "hourly_rate" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.hourly_rate == 95.0


@pytest.mark.asyncio()
async def test_update_profile_invalid_rate(
    test_contractor: ContractorData,
) -> None:
    """update_profile should return error for unparseable rates."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(hourly_rate="depends on the job")
    assert result.is_error is True
    assert "Could not parse" in result.content


@pytest.mark.asyncio()
async def test_update_profile_business_hours(
    test_contractor: ContractorData,
) -> None:
    """update_profile should update business hours."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(business_hours="Mon-Fri 7am-5pm")
    assert "business_hours" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.business_hours == "Mon-Fri 7am-5pm"


@pytest.mark.asyncio()
async def test_update_profile_valid_timezone(
    test_contractor: ContractorData,
) -> None:
    """update_profile should accept valid IANA timezones."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(timezone="America/New_York")
    assert "timezone" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.timezone == "America/New_York"


@pytest.mark.asyncio()
async def test_update_profile_invalid_timezone(
    test_contractor: ContractorData,
) -> None:
    """update_profile should reject invalid timezone strings."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(timezone="asdf")
    assert result.is_error is True
    assert "Invalid timezone" in result.content
    assert "IANA" in result.content


@pytest.mark.asyncio()
async def test_update_profile_communication_style(
    test_contractor: ContractorData,
) -> None:
    """update_profile should store communication style in preferences_json."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(communication_style="casual and brief")
    assert "communication_style" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    prefs = json.loads(refreshed.preferences_json)
    assert prefs == {"communication_style": "casual and brief"}


@pytest.mark.asyncio()
async def test_update_profile_soul_text(test_contractor: ContractorData) -> None:
    """update_profile should update soul text."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(soul_text="I specialize in deck building.")
    assert "soul_text" in result.content
    assert result.is_error is False
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.soul_text == "I specialize in deck building."


@pytest.mark.asyncio()
async def test_update_profile_multiple_fields(
    test_contractor: ContractorData,
) -> None:
    """update_profile should update multiple fields at once."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn(name="Jake", trade="Plumber", location="Portland, OR")
    assert result.is_error is False
    assert "name" in result.content
    assert "trade" in result.content
    assert "location" in result.content
    store = get_contractor_store()
    refreshed = await store.get_by_id(test_contractor.id)
    assert refreshed is not None
    assert refreshed.name == "Jake"
    assert refreshed.trade == "Plumber"
    assert refreshed.location == "Portland, OR"


@pytest.mark.asyncio()
async def test_update_profile_no_fields(test_contractor: ContractorData) -> None:
    """update_profile should return error when no fields provided."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")
    result = await update_fn()
    assert result.is_error is True
    assert "No fields provided" in result.content


@pytest.mark.asyncio()
async def test_update_profile_various_rate_formats(
    test_contractor: ContractorData,
) -> None:
    """update_profile should handle various rate formats."""
    update_fn = _get_tool_fn(test_contractor, "update_profile")

    for rate_str, expected in [
        ("$85/hour", 85.0),
        ("$4,500 per project", 4500.0),
        ("Usually around $80", 80.0),
    ]:
        result = await update_fn(hourly_rate=rate_str)
        assert result.is_error is False
        store = get_contractor_store()
        refreshed = await store.get_by_id(test_contractor.id)
        assert refreshed is not None
        assert refreshed.hourly_rate == expected


# --- extract_profile_updates_from_tool_calls tests ---


def test_extract_from_update_profile_calls() -> None:
    """Should extract profile fields from update_profile tool call records."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"name": "Mike", "trade": "Electrician"},
            result="Profile updated: name, trade",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Mike"
    assert updates["trade"] == "Electrician"


def test_extract_from_update_profile_with_rate() -> None:
    """Should extract and parse hourly rate from update_profile calls."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"hourly_rate": "$85/hr"},
            result="Profile updated: hourly_rate",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["hourly_rate"] == 85.0


def test_extract_from_update_profile_with_communication_style() -> None:
    """Should extract communication style as preferences_json."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={"communication_style": "casual and brief"},
            result="Profile updated: communication_style",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert "preferences_json" in updates
    prefs = json.loads(str(updates["preferences_json"]))
    assert prefs == {"communication_style": "casual and brief"}


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
            args={"hourly_rate": "varies"},
            result="Could not parse hourly rate",
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
            args={"trade": "Plumber", "location": "Portland"},
            result="Profile updated: trade, location",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Jake"
    assert updates["trade"] == "Plumber"
    assert updates["location"] == "Portland"


def test_extract_all_fields() -> None:
    """Should extract all supported profile fields."""
    tool_calls = [
        StoredToolInteraction(
            name="update_profile",
            args={
                "name": "Sarah",
                "trade": "Electrician",
                "location": "Austin, TX",
                "hourly_rate": "$100",
                "business_hours": "Mon-Fri 8-5",
                "communication_style": "formal",
                "soul_text": "I specialize in rewiring.",
            },
            result="Profile updated: all fields",
            is_error=False,
        ),
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Sarah"
    assert updates["trade"] == "Electrician"
    assert updates["location"] == "Austin, TX"
    assert updates["hourly_rate"] == 100.0
    assert updates["business_hours"] == "Mon-Fri 8-5"
    assert updates["soul_text"] == "I specialize in rewiring."
    prefs = json.loads(str(updates["preferences_json"]))
    assert prefs == {"communication_style": "formal"}


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
    assert "trade" in props
    assert "location" in props
    assert "hourly_rate" in props
    assert "business_hours" in props
    assert "communication_style" in props
    assert "soul_text" in props
    # No required fields since all are optional
    assert "required" not in schema
