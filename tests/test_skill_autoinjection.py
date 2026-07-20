"""Tests for first-use SKILL.md auto-injection into specialist tool results.

SKILL.md delivery via ``list_capabilities`` is opt-in; when the model calls
a specialist tool without ever fetching the category's guidance, the agent
appends the SKILL.md to that first tool result (issue #1457). Delivery is
tracked via ``[skill-guidance: <category>]`` markers scanned from history,
so guidance lands at most once per context window.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.messages import (
    AssistantMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.skills import loader
from backend.app.agent.skills.loader import skill_delivery_marker
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.registry import (
    SubToolInfo,
    ToolRegistry,
    create_list_capabilities_tool,
)
from backend.app.models import User
from tests.mocks.llm import make_text_response, make_tool_call_response

_SKILL_BODY = "## Estimation guide\nAlways confirm square footage first."
_MARKER = skill_delivery_marker("estimation")


class _QueryParams(BaseModel):
    """Single required-string params model; missing ``query`` fails validation."""

    query: str


def _noop_tool(name: str) -> Tool:
    """Build a stub tool that echoes its own name."""

    async def _run(query: str) -> ToolResult:
        return ToolResult(content=f"{name} ok")

    return Tool(name=name, description=name, function=_run, params_model=_QueryParams)


def _specialist_registry() -> ToolRegistry:
    """Build a registry with one specialist factory owning generate_estimate."""
    reg = ToolRegistry()
    reg.register(
        "estimation",
        lambda ctx: [_noop_tool("generate_estimate")],
        core=False,
        summary="Generate estimates",
        sub_tools=[SubToolInfo("generate_estimate", "Generate estimates")],
    )
    return reg


def _patched_skills() -> AbstractContextManager[dict[str, str]]:
    """Patch the loader's skill map so the estimation category has a SKILL.md."""
    return patch.dict(loader._skill_instructions, {"estimation": _SKILL_BODY})


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_first_use_appends_skill_guidance(mock_amessages: object, test_user: User) -> None:
    """The first specialist tool result carries the category's SKILL.md."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("generate_estimate")])

    with _patched_skills():
        response = await agent.process_message("estimate my deck")

    assert len(response.tool_calls) == 1
    record = response.tool_calls[0]
    assert _MARKER in record.result
    assert _SKILL_BODY in record.result
    # The follow-up LLM call must see the guidance in the tool_result block.
    followup_messages = mock_amessages.call_args_list[1].kwargs["messages"]  # type: ignore[union-attr]
    tool_result_blocks = [
        block
        for msg in followup_messages
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if block.get("type") == "tool_result"
    ]
    assert any(_SKILL_BODY in block["content"] for block in tool_result_blocks)


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_validation_error_gets_skill_guidance(
    mock_amessages: object, test_user: User
) -> None:
    """A first-use validation failure carries the guidance so the retry is informed."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "generate_estimate", "arguments": {}}]),
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("generate_estimate")])

    with _patched_skills():
        response = await agent.process_message("estimate my deck")

    assert len(response.tool_calls) == 2
    invalid_record, valid_record = response.tool_calls
    assert invalid_record.is_error
    assert _SKILL_BODY in invalid_record.result
    assert _SKILL_BODY not in valid_record.result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_reinjection_within_turn(mock_amessages: object, test_user: User) -> None:
    """A second call to the same category in a later round gets no second copy."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "fence"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("generate_estimate")])

    with _patched_skills():
        response = await agent.process_message("estimate my deck and fence")

    assert len(response.tool_calls) == 2
    assert _SKILL_BODY in response.tool_calls[0].result
    assert _SKILL_BODY not in response.tool_calls[1].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_no_reinjection_when_history_carries_marker(
    mock_amessages: object, test_user: User
) -> None:
    """A delivery marker in reloaded history suppresses injection this turn."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_text_response("done"),
    ]
    history = [
        UserMessage(content="estimate my shed"),
        AssistantMessage(
            content=None,
            tool_calls=[ToolCallRequest(id="c1", name="generate_estimate", arguments={})],
        ),
        ToolResultMessage(
            tool_call_id="c1",
            content=f"generate_estimate ok\n\n{_MARKER}\n{_SKILL_BODY}",
        ),
        AssistantMessage(content="Shed estimate sent."),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("generate_estimate")])

    with _patched_skills():
        response = await agent.process_message("now the deck", conversation_history=history)

    assert len(response.tool_calls) == 1
    assert _SKILL_BODY not in response.tool_calls[0].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_list_capabilities_lookup_suppresses_first_use_injection(
    mock_amessages: object, test_user: User
) -> None:
    """Guidance fetched via list_capabilities is not delivered a second time."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(
            [{"name": "list_capabilities", "arguments": {"category": "estimation"}}]
        ),
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    meta_tool = create_list_capabilities_tool({"estimation": "Generate estimates"})
    agent.register_tools([_noop_tool("generate_estimate"), meta_tool])

    with _patched_skills():
        response = await agent.process_message("estimate my deck")

    assert len(response.tool_calls) == 2
    lookup_record, estimate_record = response.tool_calls
    assert lookup_record.name == "list_capabilities"
    assert _SKILL_BODY in lookup_record.result
    assert _SKILL_BODY not in estimate_record.result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_core_tool_result_gets_no_guidance(mock_amessages: object, test_user: User) -> None:
    """Tools outside any specialist factory are left untouched."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "calculate", "arguments": {"query": "2+2"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("calculate")])

    with _patched_skills():
        response = await agent.process_message("what is 2+2")

    assert len(response.tool_calls) == 1
    assert "[skill-guidance:" not in response.tool_calls[0].result


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_category_without_skill_md_is_untouched(
    mock_amessages: object, test_user: User
) -> None:
    """Specialist categories with no SKILL.md get no marker appended."""
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response([{"name": "generate_estimate", "arguments": {"query": "deck"}}]),
        make_text_response("done"),
    ]
    agent = ClawboltAgent(user=test_user, registry=_specialist_registry())
    agent.register_tools([_noop_tool("generate_estimate")])

    with patch.dict(loader._skill_instructions, {}, clear=True):
        response = await agent.process_message("estimate my deck")

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].result == "generate_estimate ok"
