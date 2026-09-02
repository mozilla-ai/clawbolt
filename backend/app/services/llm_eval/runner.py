"""Orchestration for a single model-comparison run.

A run replays N of the user's most recent turns. Each turn is assembled once
and sent to both models concurrently, so the two decisions are made from a
byte-identical prompt. Per-turn rows are written as they complete rather than
in one batch at the end: a run is minutes long, and a process restart halfway
through should leave behind the evidence it already gathered instead of
nothing.

Turn failures are recorded, not raised. One provider hiccup on turn 40 must
not discard the 39 comparisons before it. Sustained failure is different: a
dead provider, a rejected key, or an exhausted quota fails every remaining
turn the same way, so after ``MAX_CONSECUTIVE_CALL_FAILURES`` turns in a row
come back with an error the run stops, keeps what it gathered, and reports
why rather than spending the rest of the samples proving the point.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, select, update

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
    JudgeSkipReason,
    JudgeVerdict,
    ModelCallResult,
    Recommendation,
    ReplaySample,
    RunStatus,
    SafetyFinding,
    SafetyIssue,
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


# How stale a cancellation check may be. Turns run concurrently and can each
# finish in well under a second, so checking per turn opened a fresh session
# per turn to read one column that changes at most once per run. A second of
# lag on a job measured in minutes is not worth that.
_CANCEL_POLL_SECONDS = 1.0


# Consecutive turns whose provider calls failed before the run gives up. A
# dead provider, a bad key, or an exhausted quota fails every remaining turn
# identically, and a 200-turn run would make 400 more calls discovering that.
# Counted consecutively rather than in total so one flaky call in a long run
# does not end it: any turn that comes back resets the count.
MAX_CONSECUTIVE_CALL_FAILURES = 3


class _CancellationWatcher:
    """Caches the cancelled flag for a run across closely-spaced checks."""

    def __init__(self, run_id: int) -> None:
        self._run_id = run_id
        self._cancelled = False
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    async def is_cancelled(self) -> bool:
        if self._cancelled:
            # Latching: a run never un-cancels, so once seen there is nothing
            # left to ask the database.
            return True
        async with self._lock:
            now = time.monotonic()
            if now - self._checked_at < _CANCEL_POLL_SECONDS:
                return self._cancelled
            async with db_session_async() as db:
                status = (
                    await db.execute(select(LLMEvalRun.status).where(LLMEvalRun.id == self._run_id))
                ).scalar_one_or_none()
            self._checked_at = now
            self._cancelled = status == RunStatus.CANCELLED
            return self._cancelled


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

    safety_issues = metrics.check_safety(
        candidate,
        baseline,
        fixture.tools_by_name,
        historic_tool_names=sample.historic_tool_names,
    )
    if baseline.error and not candidate.error:
        # ``check_safety`` only inspects the candidate, so an incumbent-side
        # provider error would otherwise leave the turn with no marker at all.
        safety_issues.append(
            SafetyIssue(
                finding=SafetyFinding.CALL_FAILED,
                detail=f"incumbent call failed: {baseline.error}",
            )
        )
    comparison = TurnComparison(
        sample=sample,
        baseline=baseline,
        candidate=candidate,
        # A turn where either call errored produced no decision to compare.
        # Classifying it anyway stores a value the models never chose (an
        # errored baseline reads as "did not act", so the turn lands in
        # ``both_replied`` or ``acted_instead_of_replying``) and sorts the
        # unmeasured turn to the bottom of the report.
        agreement=(
            AgreementClass.NOT_COMPARED
            if (baseline.error or candidate.error)
            else metrics.classify_agreement(baseline, candidate)
        ),
        safety_issues=safety_issues,
    )

    # Judge only what is both informative and still in the running: an
    # identical decision needs no opinion, a turn already disqualified by a
    # blocking finding cannot be rescued by a judge, and two models that
    # produced the same prose have nothing to separate them. That last case is
    # not just wasted spend: a verdict on it would land in the denominator of
    # the judged-worse rate and dilute the turns that matter.
    #
    # The gate is *blocking* findings, not any finding. A non-blocking mark
    # (a provider error on the incumbent side, or a tool name the fixture
    # carries but the schema no longer has) says nothing about whether the
    # candidate chose well, and skipping the judge on those left them sorted
    # to the top of the report wearing a red badge with no explanation
    # underneath it.
    same_prose = (
        comparison.agreement is AgreementClass.BOTH_REPLIED
        and baseline.text.strip() == candidate.text.strip()
    )
    skip_reason = _judge_skip_reason(
        comparison, run_has_judge=bool(run.judge_model), same_prose=same_prose
    )
    comparison.judge_skip_reason = skip_reason
    if skip_reason is None:
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


def _judge_skip_reason(
    comparison: TurnComparison, *, run_has_judge: bool, same_prose: bool
) -> str | None:
    """Why this turn was not adjudicated, or None if it should be.

    Recorded rather than inferred so the report can account for every turn.
    A summary whose judge counts add up to 26 of 40 turns, with nothing
    saying where the other 14 went, reads as a broken judge.
    """
    if not run_has_judge:
        return JudgeSkipReason.JUDGE_DISABLED
    if comparison.baseline.error or comparison.candidate.error:
        return JudgeSkipReason.CALL_FAILED
    if not comparison.diverged:
        return JudgeSkipReason.IDENTICAL
    if same_prose:
        return JudgeSkipReason.SAME_PROSE
    if comparison.has_blocking_finding:
        return JudgeSkipReason.BLOCKING_FINDING
    return None


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

    # The run knows its sample count, so the transcript read is bounded to the
    # tail it can reach rather than decrypting the user's whole history.
    fixture = await build_fixture(user, sample_limit=run.requested_samples)
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

    cancellation = _CancellationWatcher(run_id)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    comparisons: list[TurnComparison] = []
    completed = 0
    consecutive_failures = 0
    breaker_error = ""
    lock = asyncio.Lock()

    async def worker(sample: ReplaySample) -> None:
        nonlocal completed, consecutive_failures, breaker_error
        async with semaphore:
            if await cancellation.is_cancelled():
                raise asyncio.CancelledError
            if breaker_error:
                # The provider is failing every call. Return rather than
                # raise: the turns that already landed are the run's evidence
                # and ``gather`` must not discard the bookkeeping for them.
                return
            try:
                comparison = await _compare_turn(run, fixture, sample)
            except Exception as exc:
                logger.exception("Eval turn seq=%d failed in run %d", sample.seq, run_id)
                # A turn that could not even be assembled still belongs in
                # the report, as a failure rather than a silent omission.
                detail = f"{type(exc).__name__}: {exc}"
                comparison = TurnComparison(
                    sample=sample,
                    baseline=ModelCallResult(
                        provider=run.baseline_provider,
                        model=run.baseline_model,
                        error=detail,
                    ),
                    candidate=ModelCallResult(
                        provider=run.candidate_provider,
                        model=run.candidate_model,
                        error=detail,
                    ),
                    agreement=AgreementClass.NOT_COMPARED,
                    # Recorded so the turn carries the same marker as a turn
                    # whose provider call returned an error, rather than
                    # sorting to the bottom of the report with no badge. It is
                    # not a blocking finding; see ``BLOCKING_FINDINGS``.
                    safety_issues=[SafetyIssue(finding=SafetyFinding.CALL_FAILED, detail=detail)],
                    judge_verdict=JudgeVerdict.NOT_JUDGED,
                )
        failure = comparison.baseline.error or comparison.candidate.error
        async with lock:
            comparisons.append(comparison)
            completed += 1
            if failure:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_CALL_FAILURES and not breaker_error:
                    breaker_error = (
                        f"stopped after {consecutive_failures} consecutive provider "
                        f"failures: {failure}"
                    )
                    logger.warning("LLM eval run %d %s", run_id, breaker_error)
            else:
                consecutive_failures = 0
        async with db_session_async() as db:
            db.add(_turn_row(run_id, comparison))
            # Incremented in SQL rather than written from the Python counter.
            # The counter advances under the lock but the commit happens
            # outside it, so a worker holding a lower count could land last
            # and walk progress backwards. This adds exactly one per committed
            # turn regardless of the order the workers reach the database.
            await db.execute(
                update(LLMEvalRun)
                .where(LLMEvalRun.id == run_id)
                .values(progress_completed=LLMEvalRun.progress_completed + 1)
            )
            await db.commit()

    try:
        await asyncio.gather(*(worker(s) for s in samples))
    except asyncio.CancelledError:
        logger.info("LLM eval run %d cancelled after %d turns", run_id, completed)
        raise

    comparisons.sort(key=lambda c: c.sample.seq)
    aggregate = metrics.aggregate(comparisons)
    if breaker_error:
        # The evidence gathered before the provider went down is kept and
        # still readable, but it cannot endorse a switch: the run stopped
        # early, so whatever it measured is a fraction of what was asked for.
        # Both the column and the summary are stamped, or the report's banner
        # would contradict the run's status.
        aggregate.recommendation = Recommendation.INCONCLUSIVE
        aggregate.reasons = [breaker_error]
        await _finish(
            run_id,
            RunStatus.FAILED,
            recommendation=str(Recommendation.INCONCLUSIVE),
            summary=_summary_payload(aggregate),
            error=breaker_error,
        )
        logger.info(
            "LLM eval run %d abandoned after %d turn(s): %s", run_id, completed, breaker_error
        )
        return
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
        "cache_participation_ratio": round(totals.cache_participation_ratio, 4),
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
        "blocking_findings": aggregate.blocking_turns,
        "judge_counts": aggregate.judge_counts,
        "judge_skip_counts": aggregate.judge_skip_counts,
        "identical_rate": round(aggregate.identical_rate, 4),
        "divergence_rate": round(aggregate.divergence_rate, 4),
        "silent_noop_rate": round(aggregate.silent_noop_rate, 4),
        "silent_noop_blocking_rate": round(aggregate.silent_noop_blocking_rate, 4),
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
    count = cast("CursorResult[object]", result).rowcount or 0
    if count:
        logger.warning("Marked %d in-flight LLM eval run(s) as interrupted", count)
