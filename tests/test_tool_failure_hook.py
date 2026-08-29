"""Tests for the tool-failure hook seam in the agent loop.

The consumer side (consent gating, grouping, email) is covered in
``tests/multi_user/test_tool_failure_alerts.py``. What matters here is that the
agent loop actually reaches the hook, for both failure shapes, and that a
handler can never break a user's turn. Without these, the call sites in
``core.py`` could be deleted and the consumer suite would stay green.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.tool_failure_hook import (
    ToolFailurePayload,
    set_tool_failure_handler,
)
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.models import User
from tests.mocks.llm import make_text_response, make_tool_call_response


class _EmptyParams(BaseModel):
    """Minimal params model for tools with no parameters."""


@pytest.fixture(autouse=True)
def _clear_handler() -> Generator[None]:
    set_tool_failure_handler(None)
    yield
    set_tool_failure_handler(None)


@pytest.fixture
def captured() -> Generator[list[ToolFailurePayload]]:
    """Install a recording handler and hand back what it saw."""
    seen: list[ToolFailurePayload] = []

    async def handler(payload: ToolFailurePayload) -> None:
        seen.append(payload)

    set_tool_failure_handler(handler)
    yield seen


def _tool(fn: object, name: str = "broken_tool") -> Tool:
    return Tool(
        name=name,
        description="A tool for testing failures",
        function=fn,  # type: ignore[arg-type]
        params_model=_EmptyParams,
    )


async def _run(agent: ClawboltAgent, tool: Tool, mock_llm: AsyncMock) -> None:
    """Drive one tool-calling round followed by a text reply."""
    agent.register_tools([tool])
    mock_llm.side_effect = [
        make_tool_call_response([{"name": tool.name, "arguments": {}}]),
        make_text_response("done"),
    ]
    await agent.process_message(message_context="go")


@pytest.mark.asyncio
@patch("backend.app.agent.core.build_agent_system_prompt_parts", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_tool_returning_an_error_reaches_the_hook(
    mock_llm: AsyncMock,
    mock_prompt: AsyncMock,
    test_user: User,
    captured: list[ToolFailurePayload],
) -> None:
    mock_prompt.return_value = ("system", "")

    async def failing() -> ToolResult:
        return ToolResult(
            content="QuickBooks returned 503",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )

    await _run(ClawboltAgent(user=test_user), _tool(failing), mock_llm)

    assert len(captured) == 1
    payload = captured[0]
    assert payload.tool_name == "broken_tool"
    assert payload.error_kind == str(ToolErrorKind.SERVICE)
    assert payload.user_id == test_user.id
    assert payload.result_text == "QuickBooks returned 503"


@pytest.mark.asyncio
@patch("backend.app.agent.core.build_agent_system_prompt_parts", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_result_text_excludes_our_own_error_hint(
    mock_llm: AsyncMock,
    mock_prompt: AsyncMock,
    test_user: User,
    captured: list[ToolFailurePayload],
) -> None:
    """The hint is boilerplate we append. Including it would make every group's
    sample look alike and waste the sample budget."""
    mock_prompt.return_value = ("system", "")

    async def failing() -> ToolResult:
        return ToolResult(content="upstream down", is_error=True, error_kind=ToolErrorKind.SERVICE)

    await _run(ClawboltAgent(user=test_user), _tool(failing), mock_llm)

    assert captured[0].result_text == "upstream down"


@pytest.mark.asyncio
@patch("backend.app.agent.core.build_agent_system_prompt_parts", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_tool_raising_reaches_the_hook_as_internal(
    mock_llm: AsyncMock,
    mock_prompt: AsyncMock,
    test_user: User,
    captured: list[ToolFailurePayload],
) -> None:
    mock_prompt.return_value = ("system", "")

    async def exploding() -> ToolResult:
        raise RuntimeError("kaboom")

    await _run(ClawboltAgent(user=test_user), _tool(exploding), mock_llm)

    assert len(captured) == 1
    assert captured[0].error_kind == str(ToolErrorKind.INTERNAL)
    assert "kaboom" in captured[0].result_text


@pytest.mark.asyncio
@patch("backend.app.agent.core.build_agent_system_prompt_parts", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_successful_tool_does_not_reach_the_hook(
    mock_llm: AsyncMock,
    mock_prompt: AsyncMock,
    test_user: User,
    captured: list[ToolFailurePayload],
) -> None:
    mock_prompt.return_value = ("system", "")

    async def fine() -> ToolResult:
        return ToolResult(content="all good")

    await _run(ClawboltAgent(user=test_user), _tool(fine, name="good_tool"), mock_llm)

    assert captured == []


@pytest.mark.asyncio
@patch("backend.app.agent.core.build_agent_system_prompt_parts", new_callable=AsyncMock)
@patch("backend.app.agent.core.amessages")
async def test_a_raising_handler_does_not_break_the_turn(
    mock_llm: AsyncMock,
    mock_prompt: AsyncMock,
    test_user: User,
) -> None:
    """The reply must still be produced when the alerting path is broken."""
    mock_prompt.return_value = ("system", "")

    async def handler(payload: ToolFailurePayload) -> None:
        raise RuntimeError("alerting is down")

    set_tool_failure_handler(handler)

    async def failing() -> ToolResult:
        return ToolResult(content="nope", is_error=True, error_kind=ToolErrorKind.AUTH)

    agent = ClawboltAgent(user=test_user)
    agent.register_tools([_tool(failing)])
    mock_llm.side_effect = [
        make_tool_call_response([{"name": "broken_tool", "arguments": {}}]),
        make_text_response("handled it"),
    ]

    response = await agent.process_message(message_context="go")

    assert "handled it" in response.reply_text
