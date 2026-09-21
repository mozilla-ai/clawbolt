"""Model-swap evaluator: run lifecycle, per-turn results, and verdicts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.schemas.common import ReasoningEffort


class AdminLLMEvalRunCreate(BaseModel):
    """Request to replay a user's recent turns against a candidate model.

    The baseline is not accepted from the client: it is resolved server-side
    from the user's effective configuration (their subscription override, or
    the global default), so a report can never compare against a model the
    user was not actually on.
    """

    candidate_endpoint: str = Field(default="", max_length=64)
    candidate_provider: str = Field(default="", max_length=64)
    candidate_model: str = Field(min_length=1, max_length=128)
    # Empty means "the deployment's current setting", resolved and frozen
    # onto the run at creation. The two sides are independent because effort
    # does not mean the same thing to two model families.
    baseline_reasoning_effort: ReasoningEffort | Literal[""] = ""
    candidate_reasoning_effort: ReasoningEffort | Literal[""] = ""
    sample_count: int = Field(default=100, ge=1)
    judge_enabled: bool = True


class AdminLLMEvalModelTotals(BaseModel):
    """Cost, cache, and latency totals for one model across a run."""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_ratio: float = 0.0
    # Read plus written, over all billed prompt tokens. Unlike the read ratio
    # above, this does not depend on whether an earlier run left warm cache
    # entries behind, so it is the one to compare across models.
    cache_participation_ratio: float = 0.0
    total_cost_usd: str = "0.000000"
    # False when the cost above is not a cost: either genai-prices has no
    # entry for this (provider, model), or the endpoint is marked unpriced
    # because a gateway means the pair does not name who billed the tokens.
    # Zero, in both cases, must not be read as "free".
    pricing_available: bool = True
    pricing_unknown_reason: str = ""
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0


class AdminLLMEvalSummary(BaseModel):
    """The frozen aggregate stored on the run when it completed."""

    turns_total: int = 0
    turns_completed: int = 0
    turns_failed: int = 0
    agreement_counts: dict[str, int] = Field(default_factory=dict)
    safety_counts: dict[str, int] = Field(default_factory=dict)
    # Subset of ``safety_counts`` that actually disqualifies a switch. A
    # provider error is recorded above but is a failure to measure, not
    # something the candidate did, so it is excluded here.
    blocking_findings: int = 0
    judge_counts: dict[str, int] = Field(default_factory=dict)
    # Why the unjudged turns were skipped. Added to ``judge_counts`` these
    # account for every turn, so a report never leaves a silent remainder
    # between the judged count and the turn count.
    judge_skip_counts: dict[str, int] = Field(default_factory=dict)
    identical_rate: float = 0.0
    divergence_rate: float = 0.0
    silent_noop_rate: float = 0.0
    # The subset of ``silent_noop_rate`` the judge did not score in the
    # candidate's favor, which is what the recommendation blocks on. Prose is
    # the right answer to some messages, and the incumbent calling a tool
    # there is the worse decision, not the bar.
    #
    # ``None`` on a run whose summary predates the field, which is why it is
    # nullable rather than defaulting to zero: zero and "never measured" mean
    # opposite things here, and a report that read the default as zero told
    # the operator the judge had preferred no-ops it never saw.
    silent_noop_blocking_rate: float | None = None
    baseline: AdminLLMEvalModelTotals = Field(default_factory=AdminLLMEvalModelTotals)
    candidate: AdminLLMEvalModelTotals = Field(default_factory=AdminLLMEvalModelTotals)
    recommendation: str = ""
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AdminLLMEvalRunItem(BaseModel):
    """One run, without its per-turn evidence."""

    id: str
    """The run's ``public_id``. Its report is addressed by this, not by the row id."""

    user_id: str
    user_email: str = ""
    """Whose run this is, for the cross-user listing. Empty when unknown."""

    user_consented: bool = True
    """Whether this run's evidence is still readable.

    A run survives its user withdrawing data-sharing consent, but the report
    endpoint refuses it from then on. The listing says so rather than offering
    a link that 403s.
    """
    baseline_endpoint: str = ""
    baseline_provider: str
    baseline_model: str
    baseline_reasoning_effort: str = ""
    candidate_endpoint: str = ""
    candidate_provider: str
    candidate_model: str
    candidate_reasoning_effort: str = ""
    judge_model: str
    requested_samples: int
    status: str
    progress_completed: int
    progress_total: int
    recommendation: str
    error: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    summary: AdminLLMEvalSummary | None = None


class AdminLLMEvalRunListResponse(BaseModel):
    """This user's runs, plus the bounds the run form has to respect.

    ``max_samples`` is ``LLM_EVAL_MAX_SAMPLES``, which ``start_run`` enforces.
    Without it on the wire the sample control can only guess, and a deployment
    that lowers the setting gets a form offering values the API rejects.
    ``min_turns_for_verdict`` is the floor below which a run reports
    ``inconclusive`` rather than a pass, which is worth showing before someone
    spends a run finding out.
    """

    runs: list[AdminLLMEvalRunItem]
    total: int
    """Runs matching the query, not just this page, so the console can page."""

    max_samples: int
    min_turns_for_verdict: int

    max_page_size: int
    """The largest ``limit`` this endpoint accepts.

    On the wire for the same reason ``max_samples`` is: a console that grows
    its own page size past the server's ceiling gets a 422 and a table that
    stops loading, including on every subsequent poll.
    """


class AdminLLMEvalRunProgress(BaseModel):
    """Just enough to answer "is it done yet".

    Carries no conversation content and no per-turn evidence, which is what
    lets the console poll it without writing an audit row every two seconds
    for a single human read.
    """

    id: str
    status: str
    progress_completed: int
    progress_total: int
    recommendation: str


class AdminLLMEvalSafetyIssue(BaseModel):
    finding: str
    tool_name: str = ""
    detail: str = ""
    # Whether this finding disqualifies a switch on its own. Served rather
    # than re-derived client-side: the set lives in
    # ``llm_eval.metrics.BLOCKING_FINDINGS`` and a copy in the frontend was a
    # hand-maintained mirror driving whether a badge reads as an accusation.
    blocking: bool = True


class AdminLLMEvalToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AdminLLMEvalDecision(BaseModel):
    """One model's decision for one replayed turn."""

    text: str = ""
    tool_calls: list[AdminLLMEvalToolCall] = Field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    # Prompt tokens written to cache on this call. Omitting it made the two
    # columns unreadable side by side: an incumbent whose whole prompt is a
    # fresh cache write reports a few thousand ``input_tokens`` next to a
    # candidate reporting a hundred and fifty thousand, for the same prompt.
    cache_creation_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""


class AdminLLMEvalTurn(BaseModel):
    """One replayed turn: the user's message and both models' decisions.

    ``historic_reply`` and ``historic_tool_names`` are what the agent actually
    did for this turn when it happened. They are shown alongside, not scored:
    that turn ran against an older system prompt and an older tool set, so it
    is context for a human reading the diff rather than a third contestant.
    """

    message_seq: int
    message_timestamp: str
    user_message: str
    historic_reply: str = ""
    historic_tool_names: list[str] = Field(default_factory=list)
    baseline: AdminLLMEvalDecision
    candidate: AdminLLMEvalDecision
    agreement: str
    safety_issues: list[AdminLLMEvalSafetyIssue] = Field(default_factory=list)
    judge_verdict: str = "not_judged"
    judge_rationale: str = ""
    # Set when ``judge_verdict`` is ``not_judged``: which of the skip reasons
    # applied, so an unjudged turn does not read as a broken judge.
    judge_skip_reason: str = ""


class AdminLLMEvalReportResponse(BaseModel):
    """A run plus a page of its per-turn evidence, worst turns first."""

    run: AdminLLMEvalRunItem
    turns: list[AdminLLMEvalTurn]
    # Total turns stored for the run, so a caller can tell whether the page
    # it received is the whole story.
    total_turns: int = 0
