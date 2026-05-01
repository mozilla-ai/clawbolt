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
from backend.app.agent.core import (
    ClawboltAgent,
    _bucket_by_concurrency_group,
    _resolve_concurrency_group,
)
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


@pytest.mark.asyncio()
async def test_callable_concurrency_group_keys_by_args(test_user: User) -> None:
    """A callable ``concurrency_group`` lets one Tool route distinct calls
    to distinct buckets based on validated arguments.

    Workspace writers use this so two writes to *different* paths run in
    parallel while two writes to the *same* path serialize. The test runs
    three calls of the same Tool: A->"x", B->"x", C->"y". A and B must
    serialize (shared key); C must overlap with at least one of them.
    """

    class _PathParams(BaseModel):
        path: str

    log: list[str] = []
    c_started = asyncio.Event()

    async def writer(path: str) -> ToolResult:
        log.append(f"enter:{path}")
        if path == "y":
            c_started.set()
            # Hold y until both x calls have entered or finished, then exit.
            await asyncio.sleep(0.05)
        else:
            # x calls just sleep briefly to give the scheduler a chance
            # to interleave with y if it would.
            await asyncio.sleep(0.02)
        log.append(f"exit:{path}")
        return ToolResult(content=path)

    tool = Tool(
        name="write",
        description="W",
        function=writer,
        params_model=_PathParams,
        concurrency_group=lambda args: f"workspace_path:{args.get('path')}",
    )
    agent = ClawboltAgent(user=test_user)
    agent.register_tools([tool])

    parsed_calls = [
        ToolCallRequest(id="c0", name="write", arguments={"path": "x"}),
        ToolCallRequest(id="c1", name="write", arguments={"path": "x"}),
        ToolCallRequest(id="c2", name="write", arguments={"path": "y"}),
    ]
    parsed_raw = [ParsedToolCall(id=c.id, name=c.name, arguments=c.arguments) for c in parsed_calls]

    results = await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    assert [r.tool_call_id for r in results] == ["c0", "c1", "c2"]

    # Same-key calls (the two x writes) must not overlap. Locate their
    # entry/exit positions in the interleaved log and assert ordering.
    x_entries = [i for i, ev in enumerate(log) if ev == "enter:x"]
    x_exits = [i for i, ev in enumerate(log) if ev == "exit:x"]
    # Two x calls means two entries and two exits.
    assert len(x_entries) == 2 and len(x_exits) == 2
    # First x must fully complete before second x starts.
    assert x_exits[0] < x_entries[1], (
        f"same-key writes overlapped: {log!r} (first exit at {x_exits[0]}, "
        f"second enter at {x_entries[1]})"
    )

    # Different-key call (y) must run in parallel with at least one x call.
    # If the scheduler had collapsed everything into a single bucket, y
    # would only run after both x calls finished, putting "enter:y" after
    # both "exit:x" entries. Assert the opposite.
    y_entry = log.index("enter:y")
    assert y_entry < x_exits[1], (
        f"different-key call did not run in parallel: {log!r} "
        f"(y entered at {y_entry}, second x exited at {x_exits[1]})"
    )


def test_resolve_concurrency_group_handles_string_callable_and_none() -> None:
    """The resolver passes through strings, calls callables, and forwards None.

    Pure-function unit test so the contract is locked in without
    requiring an agent or any tool that already uses the field.
    """

    class _P(BaseModel):
        path: str

    async def noop(**_: object) -> ToolResult:
        return ToolResult(content="ok")

    static_tool = Tool(
        name="static",
        description="",
        function=noop,
        params_model=_P,
        concurrency_group="fixed",
    )
    callable_tool = Tool(
        name="callable",
        description="",
        function=noop,
        params_model=_P,
        concurrency_group=lambda args: f"path:{args['path']}",
    )
    none_tool = Tool(name="none", description="", function=noop, params_model=_P)

    assert _resolve_concurrency_group(static_tool, {"path": "ignored"}) == "fixed"
    assert _resolve_concurrency_group(callable_tool, {"path": "USER.md"}) == "path:USER.md"
    assert _resolve_concurrency_group(none_tool, {"path": "anything"}) is None


def test_bucket_by_concurrency_group_pure_function() -> None:
    """The bucketing helper produces the right schedule units.

    Cases exercised:
    - None-group entries each become their own (parallel) unit.
    - Entries sharing a non-None group form one (sequential) unit.
    - A callable concurrency_group that resolves to the same key for
      different entries collapses them into one bucket.
    - Order within a non-None bucket follows ``approved_entries`` order.
    """

    class _P(BaseModel):
        path: str

    async def noop(**_: object) -> ToolResult:
        return ToolResult(content="ok")

    free = Tool(name="free", description="", function=noop, params_model=_P)
    serial = Tool(
        name="serial",
        description="",
        function=noop,
        params_model=_P,
        concurrency_group="g1",
    )
    by_path = Tool(
        name="by_path",
        description="",
        function=noop,
        params_model=_P,
        concurrency_group=lambda args: f"path:{args['path']}",
    )

    approved: list[tuple[int, Tool, dict[str, object]]] = [
        (0, free, {"path": "a"}),
        (1, serial, {"path": "a"}),
        (2, by_path, {"path": "x"}),
        (3, serial, {"path": "b"}),
        (4, by_path, {"path": "x"}),
        (5, by_path, {"path": "y"}),
    ]

    units = _bucket_by_concurrency_group(approved)

    keys_by_pos = [tuple(pos for pos, _entry in unit) for unit in units]

    # Free entry (pos 0) is its own unit.
    assert (0,) in keys_by_pos
    # Serial entries (pos 1, 3) share group "g1" -> single sequential unit
    # in submission order.
    assert (1, 3) in keys_by_pos
    # Two by_path entries with path="x" (pos 2, 4) collapse via the
    # callable into one unit. pos 5 (path="y") is on its own.
    assert (2, 4) in keys_by_pos
    assert (5,) in keys_by_pos
    # Total: 4 units, covering all 6 entries exactly once.
    flat = sorted(p for unit_keys in keys_by_pos for p in unit_keys)
    assert flat == [0, 1, 2, 3, 4, 5]
    assert len(units) == 4
