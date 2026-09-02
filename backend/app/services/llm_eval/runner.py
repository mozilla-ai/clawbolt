"""Orchestration for a single model-comparison run.

A run replays N of the user's most recent turns. Each turn is assembled once
and sent to both models concurrently, so the two decisions are made from a
byte-identical prompt. Per-turn rows are written as they complete rather than
in one batch at the end: a run is minutes long, and a process restart halfway
through should leave behind the evidence it already gathered instead of
nothing.

Turn failures are recorded, not raised. One provider hiccup on turn 40 must
not discard the 39 comparisons before it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update

from backend.app.database import db_session_async
from backend.app.models import LLMEvalRun, LLMEvalTurnResult, User
from backend.app.services.llm_eval import metrics
from backend.app.services.llm_eval.execution import call_model
from backend.app.services.llm_eval.judge import judge_turn
from backend.app.services.llm_eval.sampling import (
    ReplayFixture,
    assemble_for_sample,
    build_fixture,
    select_samples,
)
from backend.app.services.llm_eval.types import (
    AgreementClass,
    JudgeVerdict,
    ModelCallResult,
    Recommendation,
    ReplaySample,
    RunStatus,
    ToolCall,
    TurnComparison,
)

logger = logging.getLogger(__name__)

# ``asyncio.create_task`` holds only a weak reference to the task it spawns,
# so without a strong reference here the GC can collect a still-running
# evaluation. Tasks drop themselves on completion.
_pending_tasks: set[asyncio.Task[None]] = set()


def launch_run(run_id: int, *, concurrency: int) -> None:
    """Start *run_id* on a background task and return immediately."""
    task = asyncio.create_task(_guarded(run_id, concurrency), name=f"llm-eval-run-{run_id}")
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _guarded(run_id: int, concurrency: int) -> None:
    """Run *run_id*, recording any unhandled failure onto the row itself.

    Without this the only trace of a crashed run would be a log line and a
    row stuck at ``running`` forever.
    """
    try:
        await execute_run(run_id, concurrency=concurrency)
    except asyncio.CancelledError:
        await _finish(run_id, RunStatus.CANCELLED, error="run cancelled")
        raise
    except Exception as exc:
        logger.exception("LLM eval run %d failed", run_id)
        await _finish(run_id, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")


async def _load_run(run_id: int) -> LLMEvalRun | None:
    async with db_session_async() as db:
        return (
            await db.execute(select(LLMEvalRun).where(LLMEvalRun.id == run_id))
        ).scalar_one_or_none()


async def _is_cancelled(run_id: int) -> bool:
    async with db_session_async() as db:
        status = (
            await db.execute(select(LLMEvalRun.status).where(LLMEvalRun.id == run_id))
        ).scalar_one_or_none()
    return status == RunStatus.CANCELLED


async def _finish(
    run_id: int,
    status: RunStatus,
    *,
    error: str = "",
    recommendation: str = "",
    summary: dict | None = None,
) -> None:
    async with db_session_async() as db:
        values: dict = {
            "status": str(status),
            "completed_at": datetime.now(UTC),
        }
        if error:
            values["error"] = error
        if recommendation:
            values["recommendation"] = recommendation
        if summary is not None:
            values["summary_json"] = summary
        await db.execute(update(LLMEvalRun).where(LLMEvalRun.id == run_id).values(**values))
        await db.commit()


def _serialize_calls(calls: list[ToolCall]) -> str:
    return json.dumps(
        [{"name": c.name, "arguments": c.arguments} for c in calls],
        default=str,
    )


def _turn_row(run_id: int, comparison: TurnComparison) -> LLMEvalTurnResult:
    sample = comparison.sample
    base = comparison.baseline
    cand = comparison.candidate
    return LLMEvalTurnResult(
        run_id=run_id,
        message_seq=sample.seq,
        message_timestamp=sample.timestamp,
        user_message=sample.message_context,
        historic_reply=sample.historic_reply,
        historic_tool_names=json.dumps(sample.historic_tool_names),
        baseline_text=base.text,
        baseline_tool_calls=_serialize_calls(base.tool_calls),
        baseline_stop_reason=base.stop_reason or "",
        baseline_input_tokens=base.input_tokens,
        baseline_output_tokens=base.output_tokens,
        baseline_cache_read_tokens=base.cache_read_input_tokens,
        baseline_cache_creation_tokens=base.cache_creation_input_tokens,
        baseline_latency_ms=base.latency_ms,
        baseline_error=base.error,
        candidate_text=cand.text,
        candidate_tool_calls=_serialize_calls(cand.tool_calls),
        candidate_stop_reason=cand.stop_reason or "",
        candidate_input_tokens=cand.input_tokens,
        candidate_output_tokens=cand.output_tokens,
        candidate_cache_read_tokens=cand.cache_read_input_tokens,
        candidate_cache_creation_tokens=cand.cache_creation_input_tokens,
        candidate_latency_ms=cand.latency_ms,
        candidate_error=cand.error,
        agreement=str(comparison.agreement),
        safety_issues=json.dumps(
            [
                {
                    "finding": str(issue.finding),
                    "tool_name": issue.tool_name,
                    "detail": issue.detail,
                }
                for issue in comparison.safety_issues
            ]
        ),
        judge_verdict=str(comparison.judge_verdict),
        judge_rationale=comparison.judge_rationale,
    )


async def _compare_turn(
    run: LLMEvalRun,
    fixture: ReplayFixture,
    sample: ReplaySample,
) -> TurnComparison:
    """Replay one turn through both models and score the result."""
    assembled = await assemble_for_sample(fixture, sample)

    baseline, candidate = await asyncio.gather(
        call_model(
            assembled,
            fixture.tool_schemas,
            provider=run.baseline_provider,
            model=run.baseline_model,
        ),
        call_model(
            assembled,
            fixture.tool_schemas,
            provider=run.candidate_provider,
            model=run.candidate_model,
        ),
    )

    comparison = TurnComparison(
        sample=sample,
        baseline=baseline,
        candidate=candidate,
        agreement=metrics.classify_agreement(baseline, candidate),
        safety_issues=metrics.check_safety(candidate, baseline, fixture.tools_by_name),
    )

    # Judge only what is both informative and still in the running: an
    # identical decision needs no opinion, and a turn already carrying a
    # safety finding is disqualified regardless of what a judge would say.
    should_judge = (
        bool(run.judge_model)
        and comparison.diverged
        and not comparison.safety_issues
        and not baseline.error
        and not candidate.error
    )
    if should_judge:
        verdict, rationale = await judge_turn(
            sample,
            baseline,
            candidate,
            provider=run.judge_provider,
            model=run.judge_model,
        )
        comparison.judge_verdict = verdict
        comparison.judge_rationale = rationale

    return comparison


async def execute_run(run_id: int, *, concurrency: int) -> None:
    """Replay the configured turns and write the run's verdict."""
    run = await _load_run(run_id)
    if run is None:
        logger.warning("LLM eval run %d disappeared before it started", run_id)
        return

    async with db_session_async() as db:
        user = (await db.execute(select(User).where(User.id == run.user_id))).scalar_one_or_none()
        if user is None:
            await _finish(run_id, RunStatus.FAILED, error="user no longer exists")
            return
        await db.execute(
            update(LLMEvalRun)
            .where(LLMEvalRun.id == run_id)
            .values(status=str(RunStatus.RUNNING), started_at=datetime.now(UTC))
        )
        await db.commit()

    fixture = await build_fixture(user)
    samples = select_samples(fixture, run.requested_samples)
    if not samples:
        await _finish(
            run_id,
            RunStatus.COMPLETED,
            recommendation=str(Recommendation.INCONCLUSIVE),
            summary=_summary_payload(metrics.aggregate([])),
            error="user has no replayable turns",
        )
        return

    async with db_session_async() as db:
        await db.execute(
            update(LLMEvalRun).where(LLMEvalRun.id == run_id).values(progress_total=len(samples))
        )
        await db.commit()

    semaphore = asyncio.Semaphore(max(1, concurrency))
    comparisons: list[TurnComparison] = []
    completed = 0
    lock = asyncio.Lock()

    async def worker(sample: ReplaySample) -> None:
        nonlocal completed
        async with semaphore:
            if await _is_cancelled(run_id):
                raise asyncio.CancelledError
            try:
                comparison = await _compare_turn(run, fixture, sample)
            except Exception as exc:
                logger.exception("Eval turn seq=%d failed in run %d", sample.seq, run_id)
                # A turn that could not even be assembled still belongs in
                # the report, as a failure rather than a silent omission.
                comparison = TurnComparison(
                    sample=sample,
                    baseline=ModelCallResult(
                        provider=run.baseline_provider, model=run.baseline_model
                    ),
                    candidate=ModelCallResult(
                        provider=run.candidate_provider,
                        model=run.candidate_model,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                    agreement=AgreementClass.BOTH_REPLIED,
                    judge_verdict=JudgeVerdict.NOT_JUDGED,
                )
        async with lock:
            comparisons.append(comparison)
            completed += 1
            current = completed
        async with db_session_async() as db:
            db.add(_turn_row(run_id, comparison))
            await db.execute(
                update(LLMEvalRun).where(LLMEvalRun.id == run_id).values(progress_completed=current)
            )
            await db.commit()

    try:
        await asyncio.gather(*(worker(s) for s in samples))
    except asyncio.CancelledError:
        logger.info("LLM eval run %d cancelled after %d turns", run_id, completed)
        raise

    comparisons.sort(key=lambda c: c.sample.seq)
    aggregate = metrics.aggregate(comparisons)
    await _finish(
        run_id,
        RunStatus.COMPLETED,
        recommendation=str(aggregate.recommendation),
        summary=_summary_payload(aggregate),
    )
    logger.info(
        "LLM eval run %d complete: %s (%d turns, %d blocking findings)",
        run_id,
        aggregate.recommendation,
        aggregate.turns_completed,
        aggregate.blocking_turns,
    )


def _model_totals_payload(totals: metrics.ModelTotals) -> dict:
    return {
        "provider": totals.provider,
        "model": totals.model,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_tokens": totals.cache_read_tokens,
        "cache_creation_tokens": totals.cache_creation_tokens,
        "cache_read_ratio": round(totals.cache_read_ratio, 4),
        "total_cost_usd": str(totals.total_cost),
        "pricing_available": totals.pricing_available,
        "latency_p50_ms": round(totals.percentile_latency_ms(0.50), 1),
        "latency_p95_ms": round(totals.percentile_latency_ms(0.95), 1),
    }


def _summary_payload(aggregate: metrics.RunAggregate) -> dict:
    """Freeze the aggregate into the JSON stored on the run row.

    Stored rather than recomputed so a threshold change in ``metrics`` never
    silently rewrites the verdict of a run an operator already acted on.
    """
    return {
        "turns_total": aggregate.turns_total,
        "turns_completed": aggregate.turns_completed,
        "turns_failed": aggregate.turns_failed,
        "agreement_counts": aggregate.agreement_counts,
        "safety_counts": aggregate.safety_counts,
        "judge_counts": aggregate.judge_counts,
        "identical_rate": round(aggregate.identical_rate, 4),
        "divergence_rate": round(aggregate.divergence_rate, 4),
        "silent_noop_rate": round(aggregate.silent_noop_rate, 4),
        "baseline": _model_totals_payload(aggregate.baseline),
        "candidate": _model_totals_payload(aggregate.candidate),
        "recommendation": str(aggregate.recommendation),
        "reasons": aggregate.reasons,
        "warnings": aggregate.warnings,
    }


async def mark_interrupted_runs() -> None:
    """Flag runs left mid-flight by a process restart.

    Called from the lifespan startup hook. A run only advances on a live
    background task, so any row still ``running`` or ``pending`` at boot
    belongs to a process that is gone.
    """
    async with db_session_async() as db:
        result = await db.execute(
            update(LLMEvalRun)
            .where(LLMEvalRun.status.in_([str(RunStatus.RUNNING), str(RunStatus.PENDING)]))
            .values(
                status=str(RunStatus.INTERRUPTED),
                completed_at=datetime.now(UTC),
                error="interrupted by a server restart",
            )
        )
        await db.commit()
    count = getattr(result, "rowcount", 0) or 0
    if count:
        logger.warning("Marked %d in-flight LLM eval run(s) as interrupted", count)
