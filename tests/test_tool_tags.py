"""Tests for the ToolTags metadata system on the Tool dataclass."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.core import BackshopAgent
from backend.app.agent.tools.base import Tool, ToolTags
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.messaging_tools import create_messaging_tools
from backend.app.models import Contractor
from tests.mocks.llm import make_text_response, make_tool_call_response

# --- ToolTags constants ---


def test_tool_tags_constants_are_strings() -> None:
    """ToolTags constants should be plain strings for JSON serialization."""
    assert isinstance(ToolTags.SENDS_REPLY, str)
    assert isinstance(ToolTags.SAVES_MEMORY, str)


def test_tool_tags_constants_are_distinct() -> None:
    """Each tag constant should be unique."""
    assert ToolTags.SENDS_REPLY != ToolTags.SAVES_MEMORY


# --- Tool dataclass tags field ---


def test_tool_default_tags_empty() -> None:
    """Tools created without explicit tags should have an empty set."""
    tool = Tool(
        name="noop",
        description="Does nothing",
        function=lambda: None,
        parameters={},
    )
    assert tool.tags == set()


def test_tool_with_single_tag() -> None:
    """A tool can be created with a single tag."""
    tool = Tool(
        name="save_fact",
        description="Saves a memory",
        function=lambda: None,
        parameters={},
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
        parameters={},
        tags={ToolTags.SAVES_MEMORY, ToolTags.SENDS_REPLY},
    )
    assert ToolTags.SAVES_MEMORY in tool.tags
    assert ToolTags.SENDS_REPLY in tool.tags


def test_tool_tags_do_not_leak_between_instances() -> None:
    """Each Tool instance should have its own tags set (no shared default)."""
    tool_a = Tool(name="a", description="A", function=lambda: None, parameters={})
    tool_b = Tool(name="b", description="B", function=lambda: None, parameters={})
    tool_a.tags.add("custom")
    assert "custom" not in tool_b.tags


# --- Tool factory tags ---


def test_memory_tools_save_fact_has_saves_memory_tag() -> None:
    """save_fact tool from create_memory_tools should have SAVES_MEMORY tag."""
    db = MagicMock()
    tools = create_memory_tools(db, contractor_id=1)
    save_fact = next(t for t in tools if t.name == "save_fact")
    assert ToolTags.SAVES_MEMORY in save_fact.tags


def test_memory_tools_recall_and_forget_have_no_special_tags() -> None:
    """recall_facts and forget_fact should not have SAVES_MEMORY or SENDS_REPLY tags."""
    db = MagicMock()
    tools = create_memory_tools(db, contractor_id=1)
    for tool in tools:
        if tool.name in ("recall_facts", "forget_fact"):
            assert ToolTags.SAVES_MEMORY not in tool.tags
            assert ToolTags.SENDS_REPLY not in tool.tags


def test_messaging_tools_have_sends_reply_tag() -> None:
    """send_reply and send_media_reply should have SENDS_REPLY tag."""
    messaging = MagicMock()
    tools = create_messaging_tools(messaging, to_address="+15550001234")
    for tool in tools:
        assert ToolTags.SENDS_REPLY in tool.tags, f"{tool.name} missing SENDS_REPLY tag"


def test_messaging_tools_do_not_have_saves_memory_tag() -> None:
    """Messaging tools should not have SAVES_MEMORY tag."""
    messaging = MagicMock()
    tools = create_messaging_tools(messaging, to_address="+15550001234")
    for tool in tools:
        assert ToolTags.SAVES_MEMORY not in tool.tags


# --- Agent core integration ---


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_tool_call_records_include_tags(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
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
    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_save = AsyncMock(return_value="Saved rate = $50/hr")
    tool = Tool(
        name="save_fact",
        description="Save a fact",
        function=mock_save,
        parameters={"type": "object", "properties": {"key": {}, "value": {}}},
        tags={ToolTags.SAVES_MEMORY},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("My rate is $50/hour")

    assert len(response.tool_calls) == 1
    assert "tags" in response.tool_calls[0]
    assert ToolTags.SAVES_MEMORY in response.tool_calls[0]["tags"]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_memories_saved_uses_tags_not_name(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
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
    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_fn = AsyncMock(return_value="Saved")
    tool = Tool(
        name="custom_memory_saver",
        description="Custom memory saver",
        function=mock_fn,
        parameters={"type": "object", "properties": {"key": {}, "value": {}}},
        tags={ToolTags.SAVES_MEMORY},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("My favorite color is blue")

    assert len(response.memories_saved) == 1
    assert response.memories_saved[0]["key"] == "color"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.acompletion")
async def test_agent_untagged_tool_has_empty_tags(
    mock_acompletion: object, db_session: Session, test_contractor: Contractor
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
    mock_acompletion.side_effect = [tool_response, followup_response]  # type: ignore[union-attr]

    mock_fn = AsyncMock(return_value="ok")
    tool = Tool(
        name="some_tool",
        description="A tool",
        function=mock_fn,
        parameters={"type": "object", "properties": {"q": {}}},
    )

    agent = BackshopAgent(db=db_session, contractor=test_contractor)
    agent.register_tools([tool])
    response = await agent.process_message("hello")

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0]["tags"] == set()
    assert len(response.memories_saved) == 0
