"""Tests for parallel tool execution in the agent loop.

Two behavioural guarantees:

1. Approved tool calls from a single LLM turn that have no
   ``concurrency_group`` (or different non-None groups) overlap when run
   together, so wall-clock time is bounded by the slowest, not the sum.
2. Approved tool calls that share the same non-None ``concurrency_group``
   serialize in submission order, so wall-clock time is at least the sum.

The tests instrument tool functions with ``asyncio.sleep`` and ``Event``
objects to make overlap (or its absence) deterministic without relying on
fragile timing windows.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.llm_parsing import ParsedToolCall
from backend.app.agent.messages import ToolCallRequest
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.models import User


class _EmptyParams(BaseModel):
    """Minimal params model for instrumented tools."""


def _make_call(idx: int, name: str) -> tuple[ToolCallRequest, ParsedToolCall]:
    """Build a matching pair of ToolCallRequest + ParsedToolCall.

    ``_execute_tool_round`` consumes both: ``parsed_calls`` for execution and
    ``parsed_raw`` only to detect malformed-args entries, which is irrelevant
    here because every test passes valid empty arguments.
    """
    tc_id = f"call_{idx}"
    return (
        ToolCallRequest(id=tc_id, name=name, arguments={}),
        ParsedToolCall(id=tc_id, name=name, arguments={}),
    )


@pytest.mark.asyncio()
async def test_concurrent_tools_overlap(test_user: User) -> None:
    """Two ungrouped tool calls fan out and finish in parallel.

    Each tool waits on a shared barrier that only releases once both have
    started. If the scheduler ran them sequentially, the second call would
    never reach the barrier and the test would deadlock (caught by the
    timeout on ``asyncio.wait_for``).
    """
    barrier = asyncio.Barrier(2)

    async def slow_a() -> ToolResult:
        await barrier.wait()
        return ToolResult(content="a-done")

    async def slow_b() -> ToolResult:
        await barrier.wait()
        return ToolResult(content="b-done")

    tools = [
        Tool(name="a", description="A", function=slow_a, params_model=_EmptyParams),
        Tool(name="b", description="B", function=slow_b, params_model=_EmptyParams),
    ]
    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)

    parsed = [_make_call(0, "a"), _make_call(1, "b")]
    parsed_calls = [p[0] for p in parsed]
    parsed_raw = [p[1] for p in parsed]

    actions: list[str] = []
    records: list[StoredToolInteraction] = []

    results = await asyncio.wait_for(
        agent._execute_tool_round(
            parsed_calls=parsed_calls,
            parsed_raw=parsed_raw,
            actions_taken=actions,
            memories_saved=[],
            tool_call_records=records,
        ),
        timeout=2.0,
    )

    assert [r.tool_call_id for r in results] == ["call_0", "call_1"]
    assert [r.content for r in results] == ["a-done", "b-done"]
    assert actions == ["Called a", "Called b"]


@pytest.mark.asyncio()
async def test_same_concurrency_group_serializes(test_user: User) -> None:
    """Tools sharing a non-None concurrency_group run sequentially.

    Records the order of entries and exits. If the scheduler fanned them
    out, ``a`` would still be inside its sleep when ``b`` entered, and the
    interleaving would show up as ``[enter-a, enter-b, ...]``. With proper
    serialization the order must be ``[enter-a, exit-a, enter-b, exit-b]``.
    """
    log: list[str] = []

    async def step_a() -> ToolResult:
        log.append("enter-a")
        await asyncio.sleep(0.05)
        log.append("exit-a")
        return ToolResult(content="a")

    async def step_b() -> ToolResult:
        log.append("enter-b")
        await asyncio.sleep(0.05)
        log.append("exit-b")
        return ToolResult(content="b")

    tools = [
        Tool(
            name="a",
            description="A",
            function=step_a,
            params_model=_EmptyParams,
            concurrency_group="shared",
        ),
        Tool(
            name="b",
            description="B",
            function=step_b,
            params_model=_EmptyParams,
            concurrency_group="shared",
        ),
    ]
    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)

    parsed = [_make_call(0, "a"), _make_call(1, "b")]
    parsed_calls = [p[0] for p in parsed]
    parsed_raw = [p[1] for p in parsed]

    results = await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    assert [r.tool_call_id for r in results] == ["call_0", "call_1"]
    assert log == ["enter-a", "exit-a", "enter-b", "exit-b"]


@pytest.mark.asyncio()
async def test_different_groups_run_in_parallel(test_user: User) -> None:
    """Tools in distinct non-None groups still fan out across groups.

    Same shape as ``test_concurrent_tools_overlap`` but with each tool in
    its own group rather than ungrouped, exercising the bucket loop.
    """
    barrier = asyncio.Barrier(2)

    async def in_group_x() -> ToolResult:
        await barrier.wait()
        return ToolResult(content="x")

    async def in_group_y() -> ToolResult:
        await barrier.wait()
        return ToolResult(content="y")

    tools = [
        Tool(
            name="x",
            description="X",
            function=in_group_x,
            params_model=_EmptyParams,
            concurrency_group="group_x",
        ),
        Tool(
            name="y",
            description="Y",
            function=in_group_y,
            params_model=_EmptyParams,
            concurrency_group="group_y",
        ),
    ]
    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)

    parsed = [_make_call(0, "x"), _make_call(1, "y")]
    parsed_calls = [p[0] for p in parsed]
    parsed_raw = [p[1] for p in parsed]

    results = await asyncio.wait_for(
        agent._execute_tool_round(
            parsed_calls=parsed_calls,
            parsed_raw=parsed_raw,
            actions_taken=[],
            memories_saved=[],
            tool_call_records=[],
        ),
        timeout=2.0,
    )

    assert [r.content for r in results] == ["x", "y"]


@pytest.mark.asyncio()
async def test_results_preserve_submission_order(test_user: User) -> None:
    """When a fast tool finishes before a slow one, results still come back
    in the order the model emitted them.

    Persisted ``StoredToolInteraction`` records are keyed by tool_call_id,
    but their order matters for the inspection UI and any consumer that
    relies on insertion order. The slow tool runs first; if we naively
    appended in completion order the fast tool would appear first.
    """

    async def slow() -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(content="slow")

    async def fast() -> ToolResult:
        return ToolResult(content="fast")

    tools = [
        Tool(name="slow", description="S", function=slow, params_model=_EmptyParams),
        Tool(name="fast", description="F", function=fast, params_model=_EmptyParams),
    ]
    agent = ClawboltAgent(user=test_user)
    agent.register_tools(tools)

    parsed = [_make_call(0, "slow"), _make_call(1, "fast")]
    parsed_calls = [p[0] for p in parsed]
    parsed_raw = [p[1] for p in parsed]

    actions: list[str] = []
    records: list[StoredToolInteraction] = []

    started = time.monotonic()
    results = await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=actions,
        memories_saved=[],
        tool_call_records=records,
    )
    elapsed = time.monotonic() - started

    assert [r.tool_call_id for r in results] == ["call_0", "call_1"]
    assert [r.content for r in results] == ["slow", "fast"]
    assert [r.name for r in records] == ["slow", "fast"]
    assert actions == ["Called slow", "Called fast"]
    # Sanity check: if the loop had been sequential, fast (instant) plus
    # slow (50ms) would still be ~50ms total. The point of this test is
    # ordering, not timing, but bound the ceiling generously.
    assert elapsed < 0.5
