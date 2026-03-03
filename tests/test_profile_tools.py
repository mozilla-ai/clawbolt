"""Tests for the update_profile tool and extract_profile_updates_from_tool_calls."""

import json

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.tools.profile_tools import (
    _parse_rate,
    create_profile_tools,
    extract_profile_updates_from_tool_calls,
)
from backend.app.models import Contractor

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


# --- update_profile tool unit tests ---


@pytest.mark.asyncio()
async def test_update_profile_name(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should update contractor name."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(name="Mike Johnson")
    assert "name" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.name == "Mike Johnson"


@pytest.mark.asyncio()
async def test_update_profile_trade(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should update contractor trade."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(trade="Electrician")
    assert "trade" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.trade == "Electrician"


@pytest.mark.asyncio()
async def test_update_profile_location(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should update contractor location."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(location="Denver, CO")
    assert "location" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.location == "Denver, CO"


@pytest.mark.asyncio()
async def test_update_profile_hourly_rate(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should parse and update hourly rate."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(hourly_rate="$85/hr")
    assert "hourly_rate" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.hourly_rate == 85.0


@pytest.mark.asyncio()
async def test_update_profile_hourly_rate_numeric(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should handle numeric hourly rate values."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(hourly_rate=95.0)
    assert "hourly_rate" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.hourly_rate == 95.0


@pytest.mark.asyncio()
async def test_update_profile_invalid_rate(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should return error for unparseable rates."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(hourly_rate="depends on the job")
    assert result.is_error is True
    assert "Could not parse" in result.content


@pytest.mark.asyncio()
async def test_update_profile_business_hours(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should update business hours."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(business_hours="Mon-Fri 7am-5pm")
    assert "business_hours" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.business_hours == "Mon-Fri 7am-5pm"


@pytest.mark.asyncio()
async def test_update_profile_communication_style(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should store communication style in preferences_json."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(communication_style="casual and brief")
    assert "communication_style" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    prefs = json.loads(test_contractor.preferences_json)
    assert prefs == {"communication_style": "casual and brief"}


@pytest.mark.asyncio()
async def test_update_profile_soul_text(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should update soul text."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(soul_text="I specialize in deck building.")
    assert "soul_text" in result.content
    assert result.is_error is False
    db_session.refresh(test_contractor)
    assert test_contractor.soul_text == "I specialize in deck building."


@pytest.mark.asyncio()
async def test_update_profile_multiple_fields(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should update multiple fields at once."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn(name="Jake", trade="Plumber", location="Portland, OR")
    assert result.is_error is False
    assert "name" in result.content
    assert "trade" in result.content
    assert "location" in result.content
    db_session.refresh(test_contractor)
    assert test_contractor.name == "Jake"
    assert test_contractor.trade == "Plumber"
    assert test_contractor.location == "Portland, OR"


@pytest.mark.asyncio()
async def test_update_profile_no_fields(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile should return error when no fields provided."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function
    result = await update_fn()
    assert result.is_error is True
    assert "No fields provided" in result.content


@pytest.mark.asyncio()
async def test_update_profile_various_rate_formats(
    db_session: Session, test_contractor: Contractor
) -> None:
    """update_profile should handle various rate formats."""
    tools = create_profile_tools(db_session, test_contractor)
    update_fn = tools[0].function

    for rate_str, expected in [
        ("$85/hour", 85.0),
        ("$4,500 per project", 4500.0),
        ("Usually around $80", 80.0),
    ]:
        result = await update_fn(hourly_rate=rate_str)
        assert result.is_error is False
        db_session.refresh(test_contractor)
        assert test_contractor.hourly_rate == expected


# --- extract_profile_updates_from_tool_calls tests ---


def test_extract_from_update_profile_calls() -> None:
    """Should extract profile fields from update_profile tool call records."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {"name": "Mike", "trade": "Electrician"},
            "result": "Profile updated: name, trade",
            "is_error": False,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Mike"
    assert updates["trade"] == "Electrician"


def test_extract_from_update_profile_with_rate() -> None:
    """Should extract and parse hourly rate from update_profile calls."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {"hourly_rate": "$85/hr"},
            "result": "Profile updated: hourly_rate",
            "is_error": False,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["hourly_rate"] == 85.0


def test_extract_from_update_profile_with_communication_style() -> None:
    """Should extract communication style as preferences_json."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {"communication_style": "casual and brief"},
            "result": "Profile updated: communication_style",
            "is_error": False,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert "preferences_json" in updates
    prefs = json.loads(str(updates["preferences_json"]))
    assert prefs == {"communication_style": "casual and brief"}


def test_extract_ignores_non_update_profile_tools() -> None:
    """Should ignore tool calls that are not update_profile."""
    tool_calls = [
        {
            "name": "save_fact",
            "args": {"key": "name", "value": "Mike"},
            "result": "Saved: name = Mike",
            "is_error": False,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates == {}


def test_extract_ignores_error_tool_calls() -> None:
    """Should ignore update_profile calls that had errors."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {"hourly_rate": "varies"},
            "result": "Could not parse hourly rate",
            "is_error": True,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates == {}


def test_extract_multiple_update_profile_calls() -> None:
    """Should merge results from multiple update_profile calls."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {"name": "Jake"},
            "result": "Profile updated: name",
            "is_error": False,
        },
        {
            "name": "update_profile",
            "args": {"trade": "Plumber", "location": "Portland"},
            "result": "Profile updated: trade, location",
            "is_error": False,
        },
    ]
    updates = extract_profile_updates_from_tool_calls(tool_calls)
    assert updates["name"] == "Jake"
    assert updates["trade"] == "Plumber"
    assert updates["location"] == "Portland"


def test_extract_all_fields() -> None:
    """Should extract all supported profile fields."""
    tool_calls = [
        {
            "name": "update_profile",
            "args": {
                "name": "Sarah",
                "trade": "Electrician",
                "location": "Austin, TX",
                "hourly_rate": "$100",
                "business_hours": "Mon-Fri 8-5",
                "communication_style": "formal",
                "soul_text": "I specialize in rewiring.",
            },
            "result": "Profile updated: all fields",
            "is_error": False,
        },
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


def test_update_profile_tool_schema(db_session: Session, test_contractor: Contractor) -> None:
    """update_profile tool should have correct name and params_model schema."""
    tools = create_profile_tools(db_session, test_contractor)
    assert len(tools) == 1
    tool = tools[0]
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
