"""End-to-end orchestration of one model-comparison run.

Model dispatch is mocked; the run's own machinery is not. What is being
tested is that the run writes its evidence as it goes, survives a turn that
fails, honors cancellation, and never executes a tool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.models import LLMEvalRun, LLMEvalTurnResult, User
from backend.app.services.llm_eval.runner import execute_run
from backend.app.services.llm_eval.sampling import ReplayFixture
from backend.app.services.llm_eval.types import (
    ModelCallResult,
    Recommendation,
    ReplaySample,
    RunStatus,
    ToolCall,
)


def _make_run(db: Session, user_id: str, *, samples: int = 3, judge: bool = False) -> int:
    run = LLMEvalRun(
        user_id=user_id,
        baseline_provider="anthropic",
        baseline_model="incumbent",
        candidate_provider="anthropic",
        candidate_model="candidate",
        judge_provider="anthropic" if judge else "",
        judge_model="incumbent" if judge else "",
        requested_samples=samples,
        status=str(RunStatus.PENDING),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run.id


def _samples(count: int) -> list[ReplaySample]:
    return [
        ReplaySample(
            seq=i,
            timestamp="2026-05-01T12:00:00+00:00",
            message_context=f"ask {i}",
            historic_reply=f"answer {i}",
            historic_tool_names=["lookup"],
        )
        for i in range(1, count + 1)
    ]


def _patched_run(
    *,
    samples: list[ReplaySample],
    call_side_effect: object,
    tools_by_name: dict | None = None,
) -> tuple[Any, Any, Any, Any]:
    """Patch the run's collaborators, leaving the orchestration real."""
    fixture = ReplayFixture(user=User(id="u"), rows=[])
    fixture.tools_by_name = tools_by_name or {}
    return (
        patch(
            "backend.app.services.llm_eval.runner.build_fixture",
            AsyncMock(return_value=fixture),
        ),
        patch(
            "backend.app.services.llm_eval.runner.select_samples",
            return_value=samples,
        ),
        patch(
            "backend.app.services.llm_eval.runner.assemble_for_sample",
            AsyncMock(return_value=object()),
        ),
        patch(
            "backend.app.services.llm_eval.runner.call_model",
            AsyncMock(side_effect=call_side_effect),
        ),
    )


class _LookupParams(BaseModel):
    q: str


def _lookup_tool(function: Callable[..., Awaitable[ToolResult]] | None = None) -> Tool:
    """A registered, non-mutating tool the models are allowed to call.

    Runs that call a tool need it present in ``tools_by_name``: an
    unregistered name is a genuine safety finding, which would sink the
    recommendation for the wrong reason.
    """

    async def _unused(**_kwargs: object) -> ToolResult:  # pragma: no cover
        raise AssertionError("a tool was executed during an evaluation")

    return Tool(
        name="lookup",
        description="lookup",
        function=function or _unused,
        params_model=_LookupParams,
    )


def _result(text: str = "", tools: list[ToolCall] | None = None) -> ModelCallResult:
    return ModelCallResult(
        provider="anthropic",
        model="m",
        text=text,
        tool_calls=tools or [],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=10,
    )


@pytest.mark.asyncio()
async def test_run_writes_a_turn_row_per_sample_and_completes(
    db_session: Session, test_user: User
) -> None:
    run_id = _make_run(db_session, test_user.id, samples=3)
    agreeing = _result(tools=[ToolCall(name="lookup", arguments={"q": "a"})])

    patches = _patched_run(
        samples=_samples(3),
        call_side_effect=lambda *a, **k: agreeing,
        tools_by_name={"lookup": _lookup_tool()},
    )
    with patches[0], patches[1], patches[2], patches[3]:
        await execute_run(run_id, concurrency=2)

    db_session.expire_all()
    run = db_session.get(LLMEvalRun, run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.progress_completed == 3
    assert run.progress_total == 3
    assert run.summary_json is not None
    assert run.summary_json["turns_completed"] == 3
    # Three matched turns is well under the minimum for a verdict, and a
    # short run must never read as permission to switch.
    assert run.recommendation == Recommendation.INCONCLUSIVE

    turns = (
        db_session.execute(select(LLMEvalTurnResult).where(LLMEvalTurnResult.run_id == run_id))
        .scalars()
        .all()
    )
    assert len(turns) == 3
    assert {t.message_seq for t in turns} == {1, 2, 3}
    stored = json.loads(turns[0].baseline_tool_calls)
    assert stored[0]["name"] == "lookup"


@pytest.mark.asyncio()
async def test_a_failing_turn_does_not_abort_the_run(db_session: Session, test_user: User) -> None:
    run_id = _make_run(db_session, test_user.id, samples=3)
    calls = {"n": 0}

    async def flaky(*_args: object, **_kwargs: object) -> ModelCallResult:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("gateway blew up")
        return _result(text="fine")

    patches = _patched_run(samples=_samples(3), call_side_effect=None)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("backend.app.services.llm_eval.runner.call_model", flaky),
    ):
        await execute_run(run_id, concurrency=1)

    db_session.expire_all()
    run = db_session.get(LLMEvalRun, run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    turns = (
        db_session.execute(select(LLMEvalTurnResult).where(LLMEvalTurnResult.run_id == run_id))
        .scalars()
        .all()
    )
    assert len(turns) == 3
    assert sum(1 for t in turns if t.candidate_error) == 1


@pytest.mark.asyncio()
async def test_cancelled_run_stops_and_keeps_the_turns_it_finished(
    db_session: Session, test_user: User
) -> None:
    run_id = _make_run(db_session, test_user.id, samples=4)

    async def cancel_after_first(*_args: object, **_kwargs: object) -> ModelCallResult:
        # Flip the row the way the cancel endpoint does, so the worker sees
        # it on its next turn.
        db_session.execute(
            update(LLMEvalRun)
            .where(LLMEvalRun.id == run_id)
            .values(status=str(RunStatus.CANCELLED))
        )
        db_session.commit()
        return _result(text="ok")

    patches = _patched_run(samples=_samples(4), call_side_effect=None)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("backend.app.services.llm_eval.runner.call_model", cancel_after_first),
        # The watcher caches the flag for a second so a run does not open a
        # session per turn to poll one column. Turns here finish in
        # microseconds, so without this the cancellation lands inside the
        # cache window and the run completes normally.
        patch("backend.app.services.llm_eval.runner._CANCEL_POLL_SECONDS", 0),
        pytest.raises(asyncio.CancelledError),
    ):
        await execute_run(run_id, concurrency=1)

    db_session.expire_all()
    turns = (
        db_session.execute(select(LLMEvalTurnResult).where(LLMEvalTurnResult.run_id == run_id))
        .scalars()
        .all()
    )
    assert len(turns) < 4


@pytest.mark.asyncio()
async def test_a_turn_that_cannot_be_replayed_is_marked_not_compared(
    db_session: Session, test_user: User
) -> None:
    """A turn that fails to assemble must keep a failure marker.

    Otherwise the hardest failure is the least visible one: it carries a
    fabricated agreement value and sorts below every turn that did run.
    """
    run_id = _make_run(db_session, test_user.id, samples=2)

    async def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("prompt could not be assembled")

    patches = _patched_run(samples=_samples(2), call_side_effect=None)
    with (
        patches[0],
        patches[1],
        patch("backend.app.services.llm_eval.runner.assemble_for_sample", explode),
        patches[3],
    ):
        await execute_run(run_id, concurrency=1)

    db_session.expire_all()
    turns = (
        db_session.execute(select(LLMEvalTurnResult).where(LLMEvalTurnResult.run_id == run_id))
        .scalars()
        .all()
    )
    assert len(turns) == 2
    for t in turns:
        assert t.agreement == "not_compared"
        assert "call_failed" in t.safety_issues
        assert "prompt could not be assembled" in t.candidate_error


@pytest.mark.asyncio()
async def test_progress_never_walks_backwards_under_concurrency(
    db_session: Session, test_user: User
) -> None:
    """Regression: the counter advanced under a lock but committed outside it,
    so a worker holding a lower count could land last."""
    run_id = _make_run(db_session, test_user.id, samples=8)
    result = _result(text="ok")
    patches = _patched_run(samples=_samples(8), call_side_effect=lambda *a, **k: result)
    with patches[0], patches[1], patches[2], patches[3]:
        await execute_run(run_id, concurrency=4)

    db_session.expire_all()
    run = db_session.get(LLMEvalRun, run_id)
    assert run is not None
    assert run.progress_completed == 8


@pytest.mark.asyncio()
async def test_run_never_executes_a_tool(db_session: Session, test_user: User) -> None:
    """The safety property the whole design rests on.

    A replay decides what a model *would* do. If a tool ever ran, an
    evaluation would text real customers and mutate real job records.
    """
    executed: list[str] = []

    async def tripwire(**_kwargs: object) -> ToolResult:
        executed.append("called")
        raise AssertionError("a tool was executed during an evaluation")

    tool = _lookup_tool(tripwire)

    run_id = _make_run(db_session, test_user.id, samples=2)
    acting = _result(tools=[ToolCall(name="lookup", arguments={"q": "anything"})])
    patches = _patched_run(
        samples=_samples(2),
        call_side_effect=lambda *a, **k: acting,
        tools_by_name={"lookup": tool},
    )
    with patches[0], patches[1], patches[2], patches[3]:
        await execute_run(run_id, concurrency=1)

    assert executed == []


@pytest.mark.asyncio()
async def test_user_with_no_turns_completes_as_inconclusive(
    db_session: Session, test_user: User
) -> None:
    run_id = _make_run(db_session, test_user.id, samples=10)
    patches = _patched_run(samples=[], call_side_effect=lambda *a, **k: _result())
    with patches[0], patches[1], patches[2], patches[3]:
        await execute_run(run_id, concurrency=1)

    db_session.expire_all()
    run = db_session.get(LLMEvalRun, run_id)
    assert run is not None
    assert run.status == RunStatus.COMPLETED
    assert run.recommendation == Recommendation.INCONCLUSIVE
    assert "no replayable turns" in run.error
