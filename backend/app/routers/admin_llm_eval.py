"""Admin endpoints for the model-swap evaluator.

An operator picks a user and a candidate model; the evaluator replays that
user's most recent turns through their current model and the candidate and
reports whether the switch looks safe. The comparison itself lives in
``backend.app.services.llm_eval``; this module owns authorization, request
validation, job lifecycle, and serialization.

Consent-gated in the same sense as ``/admin/shared-data``: a run reads the
user's real conversations and the report renders them back to an admin, so
both require ``User.data_sharing_consent``. Content is PII-redacted on the
way out, exactly as it is there. The redaction runs *after* the comparison,
so verdicts are computed on the real values and only the human-readable
drill-down is masked.

Endpoints:

- ``POST /admin/llm-eval/users/{user_id}/runs`` starts a run.
- ``GET  /admin/llm-eval/runs`` lists runs, newest first, across every user
  or one of them (``?user_id=``).
- ``GET  /admin/llm-eval/runs/{run_id}`` returns a run plus its per-turn
  evidence, worst turns first.
- ``POST /admin/llm-eval/runs/{run_id}/cancel`` stops an in-flight run.
- ``DELETE /admin/llm-eval/runs/{run_id}`` discards a run and its evidence.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.models import LLMEvalRun, LLMEvalTurnResult, Subscription, User
from backend.app.schemas import (
    AdminLLMEvalDecision,
    AdminLLMEvalReportResponse,
    AdminLLMEvalRunCreate,
    AdminLLMEvalRunItem,
    AdminLLMEvalRunListResponse,
    AdminLLMEvalSafetyIssue,
    AdminLLMEvalSummary,
    AdminLLMEvalToolCall,
    AdminLLMEvalTurn,
)
from backend.app.services.admin_audit import AdminAction, AdminAuditContext, audit_admin
from backend.app.services.llm_eval import launch_run
from backend.app.services.llm_eval.metrics import BLOCKING_FINDINGS, MIN_TURNS_FOR_VERDICT
from backend.app.services.llm_eval.types import (
    AgreementClass,
    JudgeSkipReason,
    JudgeVerdict,
    RunStatus,
)
from backend.app.services.pii_redaction import redact_pii, redact_pii_recursive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/llm-eval", tags=["admin"])

ACTIVE_STATUSES = (str(RunStatus.PENDING), str(RunStatus.RUNNING))


async def _consenting_user(user_id: str, db: AsyncSession) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.data_sharing_consent:
        raise HTTPException(
            status_code=403,
            detail="User has not consented to data sharing.",
        )
    return user


async def _effective_models(user_id: str, db: AsyncSession) -> tuple[str, str]:
    """Resolve the (provider, model) this user's agent loop runs on today.

    Mirrors ``services.llm_resolver.user_llm_override_resolver`` plus the
    agent's own fallback: either field of the override may be empty and
    falls through to the global default independently.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()
    provider = (sub.llm_provider_override if sub else "") or settings.llm_provider
    model = (sub.llm_model_override if sub else "") or settings.llm_model
    return provider, model


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _summary_of(run: LLMEvalRun) -> AdminLLMEvalSummary | None:
    if not run.summary_json:
        return None
    return AdminLLMEvalSummary.model_validate(run.summary_json)


def _run_item(
    run: LLMEvalRun, *, user_email: str = "", user_consented: bool = True
) -> AdminLLMEvalRunItem:
    return AdminLLMEvalRunItem(
        id=run.public_id,
        user_email=user_email,
        user_consented=user_consented,
        user_id=run.user_id,
        baseline_provider=run.baseline_provider,
        baseline_model=run.baseline_model,
        candidate_provider=run.candidate_provider,
        candidate_model=run.candidate_model,
        judge_model=run.judge_model,
        requested_samples=run.requested_samples,
        status=run.status,
        progress_completed=run.progress_completed,
        progress_total=run.progress_total,
        recommendation=run.recommendation,
        error=run.error,
        created_at=run.created_at.isoformat(),
        started_at=_iso(run.started_at),
        completed_at=_iso(run.completed_at),
        summary=_summary_of(run),
    )


def _load_json(raw: str, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _tool_calls(raw: str) -> list[AdminLLMEvalToolCall]:
    calls = _load_json(raw, [])
    if not isinstance(calls, list):
        return []
    return [
        AdminLLMEvalToolCall(
            name=str(entry.get("name", "")),
            arguments=redact_pii_recursive(entry.get("arguments") or {}),
        )
        for entry in calls
        if isinstance(entry, dict)
    ]


def _blocking_findings(turn: LLMEvalTurnResult) -> bool:
    """Whether this turn carries a finding that disqualifies a switch.

    ``CALL_FAILED`` and ``UNRESOLVED_TOOL_NAME`` are recorded on the turn but
    are not the candidate's fault, so neither the judge gate nor the report
    ordering may treat them as disqualifying. See ``metrics.BLOCKING_FINDINGS``.
    """
    return any(
        entry.get("finding") in BLOCKING_FINDINGS
        for entry in _load_json(turn.safety_issues, [])
        if isinstance(entry, dict)
    )


def _judge_skip_reason(turn: LLMEvalTurnResult, *, run_has_judge: bool) -> str:
    """Which skip reason left *turn* unadjudicated, or "" if it was judged.

    Derived at read time from the same signals ``runner._judge_skip_reason``
    branches on, rather than stored, so this needs no column and no migration.
    Keep the two in step: they answer the same question and a report that
    disagrees with the run is worse than one that says nothing.
    """
    if turn.judge_verdict != str(JudgeVerdict.NOT_JUDGED):
        return ""
    if not run_has_judge:
        return str(JudgeSkipReason.JUDGE_DISABLED)
    if turn.baseline_error or turn.candidate_error:
        return str(JudgeSkipReason.CALL_FAILED)
    if turn.agreement == str(AgreementClass.IDENTICAL):
        return str(JudgeSkipReason.IDENTICAL)
    if turn.agreement == str(AgreementClass.BOTH_REPLIED) and (
        turn.baseline_text.strip() == turn.candidate_text.strip()
    ):
        return str(JudgeSkipReason.SAME_PROSE)
    if _blocking_findings(turn):
        return str(JudgeSkipReason.BLOCKING_FINDING)
    return ""


def _turn_item(turn: LLMEvalTurnResult, *, run_has_judge: bool = True) -> AdminLLMEvalTurn:
    issues = _load_json(turn.safety_issues, [])
    return AdminLLMEvalTurn(
        message_seq=turn.message_seq,
        message_timestamp=turn.message_timestamp,
        user_message=redact_pii(turn.user_message),
        historic_reply=redact_pii(turn.historic_reply),
        historic_tool_names=_load_json(turn.historic_tool_names, []),
        baseline=AdminLLMEvalDecision(
            text=redact_pii(turn.baseline_text),
            tool_calls=_tool_calls(turn.baseline_tool_calls),
            stop_reason=turn.baseline_stop_reason,
            input_tokens=turn.baseline_input_tokens,
            output_tokens=turn.baseline_output_tokens,
            cache_read_tokens=turn.baseline_cache_read_tokens,
            cache_creation_tokens=turn.baseline_cache_creation_tokens,
            latency_ms=turn.baseline_latency_ms,
            error=turn.baseline_error,
        ),
        candidate=AdminLLMEvalDecision(
            text=redact_pii(turn.candidate_text),
            tool_calls=_tool_calls(turn.candidate_tool_calls),
            stop_reason=turn.candidate_stop_reason,
            input_tokens=turn.candidate_input_tokens,
            output_tokens=turn.candidate_output_tokens,
            cache_read_tokens=turn.candidate_cache_read_tokens,
            cache_creation_tokens=turn.candidate_cache_creation_tokens,
            latency_ms=turn.candidate_latency_ms,
            error=turn.candidate_error,
        ),
        agreement=turn.agreement,
        safety_issues=[
            AdminLLMEvalSafetyIssue(
                finding=str(entry.get("finding", "")),
                tool_name=str(entry.get("tool_name", "")),
                detail=redact_pii(str(entry.get("detail", ""))),
                blocking=entry.get("finding") in BLOCKING_FINDINGS,
            )
            for entry in issues
            if isinstance(entry, dict)
        ],
        judge_verdict=turn.judge_verdict,
        judge_rationale=redact_pii(turn.judge_rationale),
        judge_skip_reason=_judge_skip_reason(turn, run_has_judge=run_has_judge),
    )


# Drill-down ordering. An operator reading a report needs the turns that
# could sink the decision at the top; a hundred identical turns below them
# are reassurance, not evidence, and nobody scrolls to find the one that
# matters.
_TURN_PRIORITY = {
    str(AgreementClass.REPLIED_INSTEAD_OF_ACTING): 1,
    str(AgreementClass.DIFFERENT_TOOLS): 2,
    str(AgreementClass.SAME_TOOLS_DIFFERENT_ARGS): 3,
    str(AgreementClass.ACTED_INSTEAD_OF_REPLYING): 4,
    str(AgreementClass.BOTH_REPLIED): 5,
    str(AgreementClass.IDENTICAL): 6,
}


def _turn_sort_key(turn: LLMEvalTurnResult) -> tuple[int, int, int, int]:
    """Rank turns by how much they should change the reader's mind.

    Blocking findings rank above non-blocking ones, which is the whole point:
    a turn marked only for a retired tool name in the fixture is not evidence
    against the candidate, and ranking it first fills the readable part of the
    report with badges the summary goes on to disown.
    """
    has_blocking = 0 if _blocking_findings(turn) else 1
    judged_bad = (
        0
        if turn.judge_verdict
        in (str(JudgeVerdict.CANDIDATE_UNSAFE), str(JudgeVerdict.CANDIDATE_WORSE))
        else 1
    )
    has_advisory = 0 if _load_json(turn.safety_issues, []) else 1
    return (has_blocking, judged_bad, has_advisory, _TURN_PRIORITY.get(turn.agreement, 7))


@router.post("/users/{user_id}/runs", response_model=AdminLLMEvalRunItem, status_code=201)
async def start_run(
    user_id: str,
    payload: AdminLLMEvalRunCreate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.START_LLM_EVAL_RUN)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminLLMEvalRunItem:
    """Queue a comparison run and return the row immediately.

    The run executes on a background task; poll the returned ``id`` for
    progress. One active run per user: a second concurrent replay of the
    same history would double the provider load for no extra information.
    """
    user = await _consenting_user(user_id, db)
    ctx.target_user_id = user.id

    if payload.sample_count > settings.llm_eval_max_samples:
        raise HTTPException(
            status_code=422,
            detail=f"sample_count exceeds the configured maximum of "
            f"{settings.llm_eval_max_samples}",
        )

    active = (
        await db.execute(
            select(LLMEvalRun.id)
            .where(LLMEvalRun.user_id == user_id)
            .where(LLMEvalRun.status.in_(ACTIVE_STATUSES))
        )
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="An evaluation is already running for this user.",
        )

    # Runs compete with live inbound traffic for the same provider rate limit,
    # so the per-user guard above is not enough on its own.
    running_total = (
        await db.execute(
            select(sa_func.count(LLMEvalRun.id)).where(LLMEvalRun.status.in_(ACTIVE_STATUSES))
        )
    ).scalar_one()
    if running_total >= settings.llm_eval_max_concurrent_runs:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{running_total} evaluation(s) already running; the limit is "
                f"{settings.llm_eval_max_concurrent_runs}. Try again when one finishes."
            ),
        )

    baseline_provider, baseline_model = await _effective_models(user_id, db)
    if not baseline_model:
        raise HTTPException(
            status_code=422,
            detail="No baseline model is configured; set the global LLM model first.",
        )

    run = LLMEvalRun(
        user_id=user_id,
        created_by_admin_id=ctx.admin_user_id,
        baseline_provider=baseline_provider,
        baseline_model=baseline_model,
        candidate_provider=payload.candidate_provider,
        candidate_model=payload.candidate_model,
        # The incumbent judges, since it is the behavior being defended.
        # ``judge_turn`` blinds and shuffles the two responses so it cannot
        # simply vote for itself.
        judge_provider=baseline_provider if payload.judge_enabled else "",
        judge_model=baseline_model if payload.judge_enabled else "",
        requested_samples=payload.sample_count,
        status=str(RunStatus.PENDING),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    launch_run(run.id, concurrency=settings.llm_eval_concurrency)
    logger.info(
        "Started LLM eval run %d for user %s: %s/%s vs %s/%s over %d turns",
        run.id,
        user_id,
        baseline_provider,
        baseline_model,
        payload.candidate_provider,
        payload.candidate_model,
        payload.sample_count,
    )
    return _run_item(run)


@router.get("/runs", response_model=AdminLLMEvalRunListResponse)
async def list_runs(
    user_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_EVAL_RUNS)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminLLMEvalRunListResponse:
    """Evaluation runs, newest first, across every user or one of them.

    Unfiltered by default so the console can answer "what has been evaluated
    lately", which is how an operator finds a run again weeks later without
    remembering whose it was. ``user_id`` narrows it to one user for the run
    form beside it.

    No consent gate here, unlike the report: a row is run metadata (which
    models, what verdict, how many turns), not the user's conversations. Each
    row carries ``user_consented`` so the console can show that a run's
    evidence is no longer readable rather than offering a link that 403s.
    """
    query = select(LLMEvalRun).order_by(desc(LLMEvalRun.created_at))
    total_query = select(sa_func.count()).select_from(LLMEvalRun)
    if user_id is not None:
        # Existence still matters: a typo'd id should 404 rather than quietly
        # return an empty list that reads as "this user has never been run".
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        ctx.target_user_id = user.id
        query = query.where(LLMEvalRun.user_id == user_id)
        total_query = total_query.where(LLMEvalRun.user_id == user_id)

    runs = (await db.execute(query.limit(limit).offset(offset))).scalars().all()
    total = await db.scalar(total_query) or 0

    # One query for the identities on this page rather than one per row.
    owner_ids = {r.user_id for r in runs}
    emails: dict[str, str] = {}
    consented: dict[str, bool] = {}
    if owner_ids:
        rows = (
            await db.execute(
                select(User.id, User.data_sharing_consent, Subscription.email)
                .outerjoin(Subscription, Subscription.user_id == User.id)
                .where(User.id.in_(owner_ids))
            )
        ).all()
        for owner_id, consent, email in rows:
            emails[owner_id] = email or ""
            consented[owner_id] = bool(consent)

    return AdminLLMEvalRunListResponse(
        runs=[
            _run_item(
                r,
                user_email=emails.get(r.user_id, ""),
                user_consented=consented.get(r.user_id, False),
            )
            for r in runs
        ],
        total=total,
        max_samples=settings.llm_eval_max_samples,
        min_turns_for_verdict=MIN_TURNS_FOR_VERDICT,
    )


@router.get("/runs/{run_id}", response_model=AdminLLMEvalReportResponse)
async def get_report(
    run_id: str,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_EVAL_REPORT)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminLLMEvalReportResponse:
    """Return a run and a page of its evidence, most concerning turns first.

    Paged because every text column on a turn is envelope-encrypted and then
    PII-redacted: serializing a 200-turn run whole is roughly twelve hundred
    decrypts for a single page view. The ordering is what makes a page worth
    reading, so the sort runs across the whole run and the page is taken from
    the result, not the other way round.
    """
    run = (
        await db.execute(select(LLMEvalRun).where(LLMEvalRun.public_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _consenting_user(run.user_id, db)
    ctx.target_user_id = run.user_id

    turns = (
        (await db.execute(select(LLMEvalTurnResult).where(LLMEvalTurnResult.run_id == run.id)))
        .scalars()
        .all()
    )
    ordered = sorted(turns, key=_turn_sort_key)
    page = ordered[offset : offset + limit]
    return AdminLLMEvalReportResponse(
        run=_run_item(run),
        turns=[_turn_item(t, run_has_judge=bool(run.judge_model)) for t in page],
        total_turns=len(ordered),
    )


@router.post("/runs/{run_id}/cancel", response_model=AdminLLMEvalRunItem)
async def cancel_run(
    run_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.CANCEL_LLM_EVAL_RUN)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminLLMEvalRunItem:
    """Ask an in-flight run to stop.

    Flips the status; the worker checks it between turns and unwinds. Turns
    already written stay, so a cancelled run keeps whatever evidence it had
    gathered.
    """
    run = (
        await db.execute(select(LLMEvalRun).where(LLMEvalRun.public_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ctx.target_user_id = run.user_id
    if run.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}.")
    run.status = str(RunStatus.CANCELLED)
    await db.commit()
    await db.refresh(run)
    return _run_item(run)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DELETE_LLM_EVAL_RUN)),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """Discard a run and every turn it recorded.

    Runs accumulate: a verdict is only as good as the harness that produced
    it, so a scoring change strands every earlier run at a number nobody
    should act on. Leaving them listed is worse than losing them, because the
    console sorts newest-first and an operator reading a stale
    ``do_not_switch`` has no way to tell it was measured by since-fixed code.

    Not consent-gated, unlike the report. A run belonging to a user who has
    since withdrawn consent is exactly the run most worth removing, and a
    gate here would pin it in the list permanently.

    Refuses while the run is still going. Its workers are mid-flight against
    a paid provider, and deleting under them throws that spend away for a
    result nobody asked to abandon, so stopping the run is a decision the
    operator makes explicitly: cancel first, then delete. A worker that is
    already inside a turn when the row goes away unwinds quietly; see the
    ``IntegrityError`` branch in ``llm_eval.runner``.

    The turn results go with the run through
    ``llm_eval_turn_results.run_id``'s ``ON DELETE CASCADE``. The audit row
    this request writes survives, and is the only remaining evidence the run
    was ever here.
    """
    run = (
        await db.execute(select(LLMEvalRun).where(LLMEvalRun.public_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ctx.target_user_id = run.user_id
    if run.status in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Run is still {run.status}. Cancel it before deleting.",
        )
    await db.delete(run)
    await db.commit()
