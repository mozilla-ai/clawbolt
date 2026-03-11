"""Tests for the ToolTags metadata system on the Tool dataclass."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import UserData
from backend.app.agent.tools.base import Tool, ToolResult, ToolTags
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.messaging_tools import create_messaging_tools
from tests.mocks.llm import make_text_response, make_tool_call_response


class _EmptyParams(BaseModel):
    """Minimal params model for tools with no parameters."""


class _KeyValueParams(BaseModel):
    """Params model for tools accepting key/value pairs."""

    key: str
    value: str


class _QParams(BaseModel):
    """Params model for tools accepting a q parameter."""

    q: str


# --- ToolTags constants ---


def test_tool_tags_is_str_enum() -> None:
    """ToolTags should be a StrEnum for type safety with string backward compat."""
    from enum import StrEnum

    assert issubclass(ToolTags, StrEnum)
    assert isinstance(ToolTags.SENDS_REPLY, str)
    assert isinstance(ToolTags.SAVES_MEMORY, str)
    assert isinstance(ToolTags.MODIFIES_PROFILE, str)


def test_tool_tags_constants_are_distinct() -> None:
    """Each tag constant should be unique."""
    assert ToolTags.SENDS_REPLY != ToolTags.SAVES_MEMORY


def test_tool_tags_values_equal_plain_strings() -> None:
    """StrEnum values should compare equal to plain strings for backward compat."""
    assert ToolTags.SENDS_REPLY == "sends_reply"
    assert ToolTags.SAVES_MEMORY == "saves_memory"
    assert ToolTags.MODIFIES_PROFILE == "modifies_profile"


def test_tool_tags_membership_with_plain_strings() -> None:
    """StrEnum values should be found in sets of plain strings and vice versa."""
    assert ToolTags.SENDS_REPLY in {"sends_reply"}
    assert "sends_reply" in {ToolTags.SENDS_REPLY}


# --- Tool dataclass tags field ---


def test_tool_default_tags_empty() -> None:
    """Tools created without explicit tags should have an empty set."""
    tool = Tool(
        name="noop",
        description="Does nothing",
        function=lambda: None,
        params_model=_EmptyParams,
    )
    assert tool.tags == set()


def test_tool_with_single_tag() -> None:
    """A tool can be created with a single tag."""
    tool = Tool(
        name="save_fact",
        description="Saves a memory",
        function=lambda: None,
        params_model=_EmptyParams,
        tags={ToolTags.SAVES_MEMORY},
    )
    assert ToolTags.SAVES_MEMORY in tool.tags
    assert ToolTags.SENDS_REPLY not in tool.tags


def test_tool_with_multiple_tags() -> None:
    """A tool can have multiple tags."""
    tool = Tool(
        name="multi",
        description="Multi-purpose",
        function=lambda: None,
        params_model=_EmptyParams,
        tags={ToolTags.SAVES_MEMORY, ToolTags.SENDS_REPLY},
    )
    assert ToolTags.SAVES_MEMORY in tool.tags
    assert ToolTags.SENDS_REPLY in tool.tags


def test_tool_tags_do_not_leak_between_instances() -> None:
    """Each Tool instance should have its own tags set (no shared default)."""
    tool_a = Tool(name="a", description="A", function=lambda: None, params_model=_EmptyParams)
    tool_b = Tool(name="b", description="B", function=lambda: None, params_model=_EmptyParams)
    tool_a.tags.add(ToolTags.SAVES_MEMORY)
    assert ToolTags.SAVES_MEMORY not in tool_b.tags


# --- Tool factory tags ---


def test_memory_tools_save_fact_has_saves_memory_tag() -> None:
    """save_fact tool from create_memory_tools should have SAVES_MEMORY tag."""
    tools = create_memory_tools(user_id=1)
    save_fact = next(t for t in tools if t.name == "save_fact")
    assert ToolTags.SAVES_MEMORY in save_fact.tags


def test_memory_tools_recall_and_forget_have_no_special_tags() -> None:
    """recall_facts and forget_fact should not have SAVES_MEMORY or SENDS_REPLY tags."""
    tools = create_memory_tools(user_id=1)
    for tool in tools:
        if tool.name in ("recall_facts", "forget_fact"):
            assert ToolTags.SAVES_MEMORY not in tool.tags
            assert ToolTags.SENDS_REPLY not in tool.tags


def test_messaging_tools_have_sends_reply_tag() -> None:
    """send_reply and send_media_reply should have SENDS_REPLY tag."""
    publish_outbound = AsyncMock()
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="+15550001234")
    for tool in tools:
        assert ToolTags.SENDS_REPLY in tool.tags, f"{tool.name} missing SENDS_REPLY tag"


def test_messaging_tools_do_not_have_saves_memory_tag() -> None:
    """Messaging tools should not have SAVES_MEMORY tag."""
    publish_outbound = AsyncMock()
    tools = create_messaging_tools(publish_outbound, channel="telegram", to_address="+15550001234")
    for tool in tools:
        assert ToolTags.SAVES_MEMORY not in tool.tags


# --- Agent core integration ---


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_tool_call_records_include_tags(
    mock_amessages: object, test_user: UserData
) -> None:
    """Tool call records in AgentResponse should include tags from the Tool definition."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "name": "save_fact",
                "arguments": json.dumps({"key": "rate", "value": "$50/hr"}),
            }
        ]
    )
    followup_response = make_text_response("Got it!")
    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_save = AsyncMock(return_value=ToolResult(content="Saved rate = $50/hr"))
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        params_model=_KeyValueParams,
        tags={ToolTags.SAVES_MEMORY},
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $50/hour")

    assert len(response.tool_calls) == 1
    assert ToolTags.SAVES_MEMORY in response.tool_calls[0].tags


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_memories_saved_uses_tags_not_name(
    mock_amessages: object, test_user: UserData
) -> None:
    """memories_saved should be populated based on SAVES_MEMORY tag, not tool name."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "name": "custom_memory_saver",
                "arguments": json.dumps({"key": "color", "value": "blue"}),
            }
        ]
    )
    followup_response = make_text_response("Noted!")
    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_fn = AsyncMock(return_value=ToolResult(content="Saved"))
    tool = Tool(
        name="custom_memory_saver",
        description="Custom memory saver",
        function=mock_fn,
        params_model=_KeyValueParams,
        tags={ToolTags.SAVES_MEMORY},
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("My favorite color is blue")

    assert len(response.memories_saved) == 1
    assert response.memories_saved[0]["key"] == "color"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_untagged_tool_has_empty_tags(
    mock_amessages: object, test_user: UserData
) -> None:
    """Tool without tags should produce tool_call record with empty tags set."""
    tool_response = make_tool_call_response(
        tool_calls=[
            {
                "name": "some_tool",
                "arguments": json.dumps({"q": "hello"}),
            }
        ]
    )
    followup_response = make_text_response("Done!")
    mock_amessages.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_fn = AsyncMock(return_value=ToolResult(content="ok"))
    tool = Tool(
        name="some_tool",
        description="A tool",
        function=mock_fn,
        params_model=_QParams,
    )

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])
    response = await agent.process_message("hello")

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tags == set()
    assert len(response.memories_saved) == 0


# --- Workspace tools tags ---


def test_delete_file_has_modifies_profile_tag() -> None:
    """delete_file tool should have MODIFIES_PROFILE tag."""
    from backend.app.agent.tools.workspace_tools import create_workspace_tools

    tools = create_workspace_tools(user_id=1)
    delete_file = next(t for t in tools if t.name == "delete_file")
    assert ToolTags.MODIFIES_PROFILE in delete_file.tags


def test_read_file_has_no_modifies_profile_tag() -> None:
    """read_file should not have MODIFIES_PROFILE tag."""
    from backend.app.agent.tools.workspace_tools import create_workspace_tools

    tools = create_workspace_tools(user_id=1)
    read_file = next(t for t in tools if t.name == "read_file")
    assert ToolTags.MODIFIES_PROFILE not in read_file.tags
