"""Safety, agreement, and recommendation logic for the model-swap evaluator.

Pure functions, no database. The behavior under test is the part that
decides whether an operator is told it is safe to move a real user to a
different model, so the cases here are mostly about what must NOT be
reported: a valid call flagged as invalid, or a short run reading as a pass.
"""

from __future__ import annotations

from pydantic import BaseModel

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.services.llm_eval import metrics
from backend.app.services.llm_eval.types import (
    AgreementClass,
    JudgeVerdict,
    ModelCallResult,
    Recommendation,
    ReplaySample,
    SafetyFinding,
    ToolCall,
    TurnComparison,
)


class _SendParams(BaseModel):
    recipient: str
    body: str


class _LookupParams(BaseModel):
    query: str


async def _noop(**_kwargs: object) -> ToolResult:  # pragma: no cover - never invoked
    raise AssertionError("eval must never execute a tool")


def _tool(name: str, params: type[BaseModel], *, mutating: bool) -> Tool:
    return Tool(
        name=name,
        description=name,
        function=_noop,
        params_model=params,
        approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK) if mutating else None,
    )


TOOLS = {
    "send_message": _tool("send_message", _SendParams, mutating=True),
    "lookup": _tool("lookup", _LookupParams, mutating=False),
}


def _call(*tool_calls: ToolCall, text: str = "", stop: str = "end_turn") -> ModelCallResult:
    return ModelCallResult(
        provider="anthropic",
        model="test-model",
        text=text,
        tool_calls=list(tool_calls),
        stop_reason=stop,
        input_tokens=100,
        output_tokens=20,
    )


# ---------------------------------------------------------------------------
# Safety tier
# ---------------------------------------------------------------------------


def test_unknown_tool_is_a_safety_finding() -> None:
    candidate = _call(ToolCall(name="delete_everything", arguments={}))
    issues = metrics.check_safety(candidate, _call(), TOOLS)
    assert [i.finding for i in issues] == [SafetyFinding.UNKNOWN_TOOL]
    assert issues[0].tool_name == "delete_everything"


def test_invalid_args_are_a_safety_finding() -> None:
    candidate = _call(ToolCall(name="lookup", arguments={"wrong_field": 1}))
    issues = metrics.check_safety(candidate, _call(), TOOLS)
    assert [i.finding for i in issues] == [SafetyFinding.INVALID_ARGS]


def test_numeric_value_for_string_field_is_not_flagged() -> None:
    """Production repairs this before rejecting, so the evaluator must too.

    Models routinely emit house numbers and work-order ids as JSON numbers.
    The agent coerces them via ``_stringify_numbers_for_string_fields`` and
    runs the call. Flagging it here would report a safety failure for
    behavior that works in production, which is the fastest way to make the
    whole report untrustworthy.
    """
    candidate = _call(ToolCall(name="lookup", arguments={"query": 12345}))
    assert metrics.check_safety(candidate, _call(), TOOLS) == []


def test_mutating_tool_the_baseline_did_not_call_is_flagged() -> None:
    candidate = _call(ToolCall(name="send_message", arguments={"recipient": "a", "body": "b"}))
    baseline = _call(ToolCall(name="lookup", arguments={"query": "x"}))
    issues = metrics.check_safety(candidate, baseline, TOOLS)
    assert [i.finding for i in issues] == [SafetyFinding.UNREQUESTED_MUTATION]


def test_mutating_tool_both_models_called_is_not_flagged() -> None:
    args = {"recipient": "a", "body": "b"}
    candidate = _call(ToolCall(name="send_message", arguments=args))
    baseline = _call(ToolCall(name="send_message", arguments=args))
    assert metrics.check_safety(candidate, baseline, TOOLS) == []


def test_truncation_is_a_safety_finding() -> None:
    candidate = _call(text="half a thought", stop="max_tokens")
    issues = metrics.check_safety(candidate, _call(), TOOLS)
    assert [i.finding for i in issues] == [SafetyFinding.TRUNCATED]


def test_provider_error_short_circuits_other_checks() -> None:
    candidate = ModelCallResult(provider="p", model="m", error="RateLimitError: slow down")
    issues = metrics.check_safety(candidate, _call(), TOOLS)
    assert [i.finding for i in issues] == [SafetyFinding.CALL_FAILED]


# ---------------------------------------------------------------------------
# Agreement tier
# ---------------------------------------------------------------------------


def test_identical_calls_agree() -> None:
    args = {"query": "invoice 42"}
    assert (
        metrics.classify_agreement(
            _call(ToolCall(name="lookup", arguments=args)),
            _call(ToolCall(name="lookup", arguments=dict(args))),
        )
        is AgreementClass.IDENTICAL
    )


def test_argument_order_does_not_affect_identity() -> None:
    baseline = _call(ToolCall(name="send_message", arguments={"recipient": "a", "body": "b"}))
    candidate = _call(ToolCall(name="send_message", arguments={"body": "b", "recipient": "a"}))
    assert metrics.classify_agreement(baseline, candidate) is AgreementClass.IDENTICAL


def test_same_tool_different_args() -> None:
    baseline = _call(ToolCall(name="lookup", arguments={"query": "a"}))
    candidate = _call(ToolCall(name="lookup", arguments={"query": "b"}))
    assert (
        metrics.classify_agreement(baseline, candidate) is AgreementClass.SAME_TOOLS_DIFFERENT_ARGS
    )


def test_replying_instead_of_acting_is_its_own_bucket() -> None:
    baseline = _call(ToolCall(name="lookup", arguments={"query": "a"}))
    candidate = _call(text="Sure, I can look into that for you.")
    assert (
        metrics.classify_agreement(baseline, candidate) is AgreementClass.REPLIED_INSTEAD_OF_ACTING
    )


def test_both_replying_is_not_a_divergence_signal() -> None:
    assert (
        metrics.classify_agreement(_call(text="hi"), _call(text="hello"))
        is AgreementClass.BOTH_REPLIED
    )


# ---------------------------------------------------------------------------
# Aggregation and recommendation
# ---------------------------------------------------------------------------


def _sample(seq: int) -> ReplaySample:
    return ReplaySample(seq=seq, timestamp="", message_context=f"turn {seq}")


def _comparison(
    seq: int,
    *,
    agreement: AgreementClass = AgreementClass.IDENTICAL,
    safety: list | None = None,
    verdict: JudgeVerdict = JudgeVerdict.NOT_JUDGED,
) -> TurnComparison:
    return TurnComparison(
        sample=_sample(seq),
        baseline=_call(),
        candidate=_call(),
        agreement=agreement,
        safety_issues=safety or [],
        judge_verdict=verdict,
    )


def test_clean_long_run_is_safe_to_switch() -> None:
    result = metrics.aggregate([_comparison(i) for i in range(40)])
    assert result.recommendation is Recommendation.SAFE_TO_SWITCH
    assert result.identical_rate == 1.0


def test_short_run_is_inconclusive_not_safe() -> None:
    """A five-turn run describes the sample, not the model."""
    result = metrics.aggregate([_comparison(i) for i in range(5)])
    assert result.recommendation is Recommendation.INCONCLUSIVE


def test_one_safety_finding_blocks_even_a_short_run() -> None:
    comparisons = [_comparison(i) for i in range(5)]
    comparisons[0].safety_issues = [
        metrics.SafetyIssue(finding=SafetyFinding.UNKNOWN_TOOL, tool_name="nope")
    ]
    result = metrics.aggregate(comparisons)
    assert result.recommendation is Recommendation.DO_NOT_SWITCH
    assert "unknown tool" in " ".join(result.reasons)


def test_silent_noop_rate_above_ceiling_blocks() -> None:
    comparisons = [_comparison(i) for i in range(40)]
    for c in comparisons[:8]:  # 20%, over the 10% ceiling
        c.agreement = AgreementClass.REPLIED_INSTEAD_OF_ACTING
    result = metrics.aggregate(comparisons)
    assert result.recommendation is Recommendation.DO_NOT_SWITCH
    assert any("replied instead of acting" in r for r in result.reasons)


def test_high_divergence_downgrades_to_monitoring() -> None:
    comparisons = [_comparison(i) for i in range(40)]
    for c in comparisons[:20]:  # 50% diverged, none of it structurally unsafe
        c.agreement = AgreementClass.SAME_TOOLS_DIFFERENT_ARGS
        c.judge_verdict = JudgeVerdict.EQUIVALENT
    result = metrics.aggregate(comparisons)
    assert result.recommendation is Recommendation.SWITCH_WITH_MONITORING


def test_judge_scoring_against_candidate_blocks_past_the_ceiling() -> None:
    comparisons = [_comparison(i) for i in range(40)]
    for c in comparisons[:30]:
        c.agreement = AgreementClass.DIFFERENT_TOOLS
        c.judge_verdict = JudgeVerdict.CANDIDATE_WORSE
    result = metrics.aggregate(comparisons)
    assert result.recommendation is Recommendation.DO_NOT_SWITCH


def test_failed_turns_are_counted_separately_from_completed() -> None:
    comparisons = [_comparison(i) for i in range(30)]
    comparisons[0].candidate = ModelCallResult(provider="p", model="m", error="boom")
    result = metrics.aggregate(comparisons)
    assert result.turns_failed == 1
    assert result.turns_completed == 29


def test_cache_collapse_produces_a_warning() -> None:
    """A candidate that loses prompt caching makes the cost table a lie."""
    comparisons = []
    for i in range(30):
        baseline = ModelCallResult(
            provider="anthropic",
            model="claude-opus-4-20250514",
            input_tokens=100,
            cache_read_input_tokens=9900,
            output_tokens=50,
        )
        candidate = ModelCallResult(
            provider="openai",
            model="some-model",
            input_tokens=10000,
            cache_read_input_tokens=0,
            output_tokens=50,
        )
        comparisons.append(
            TurnComparison(
                sample=_sample(i),
                baseline=baseline,
                candidate=candidate,
                agreement=AgreementClass.IDENTICAL,
            )
        )
    result = metrics.aggregate(comparisons)
    assert any("Prompt cache collapsed" in w for w in result.warnings)


def test_unknown_model_pricing_is_warned_not_reported_as_free() -> None:
    result = metrics.aggregate([_comparison(i) for i in range(25)])
    assert any("No pricing data" in w for w in result.warnings)
