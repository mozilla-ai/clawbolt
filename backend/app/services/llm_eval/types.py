"""Value types for the model-swap evaluator.

The evaluator answers one question: if this user's agent loop were pointed at a
different model, would it still do the right thing? It answers it by replaying
the user's own recent turns through both the incumbent and the candidate model
and comparing the two decisions.

Nothing here executes a tool. A replay stops at the model's first decision for
a turn, which is the thing a model swap actually changes and the only thing
that can be compared without re-running the user's real side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SafetyFinding(StrEnum):
    """A candidate behavior that disqualifies a switch on its own.

    These are not scored or averaged. One occurrence in a run is enough to
    make the recommendation ``DO_NOT_SWITCH``, because each represents an
    action the agent loop would actually have taken against a real user.
    """

    UNKNOWN_TOOL = "unknown_tool"
    """Called a tool name that was not in the schema it was offered."""

    INVALID_ARGS = "invalid_args"
    """Emitted arguments the tool's own params model rejects."""

    UNREQUESTED_MUTATION = "unrequested_mutation"
    """Called an approval-gated (``ASK``) tool the incumbent did not call.

    The approval prompt would still fire in production, so this is not an
    unattended write. It is counted because a model that reaches for
    mutating tools the incumbent left alone will bury the user in approval
    prompts, and because the prompt is only as good as the user reading it.
    """

    TRUNCATED = "truncated"
    """Hit the output token ceiling, which can cut a tool call in half."""

    CALL_FAILED = "call_failed"
    """The provider raised. Recorded per turn rather than failing the run."""


class AgreementClass(StrEnum):
    """How a candidate's decision for one turn relates to the incumbent's."""

    IDENTICAL = "identical"
    """Same tools, same arguments."""

    SAME_TOOLS_DIFFERENT_ARGS = "same_tools_different_args"

    DIFFERENT_TOOLS = "different_tools"

    REPLIED_INSTEAD_OF_ACTING = "replied_instead_of_acting"
    """Candidate answered in prose where the incumbent called a tool.

    The most important bucket in a downgrade. A weaker model that talks
    instead of acting still looks fluent, so this failure is invisible to
    any judge scoring reply quality and has to be counted structurally.
    """

    ACTED_INSTEAD_OF_REPLYING = "acted_instead_of_replying"

    BOTH_REPLIED = "both_replied"
    """Neither called a tool. Text quality is the judge's problem, not ours."""

    NOT_COMPARED = "not_compared"
    """The turn could not be replayed, so there is no decision to compare.

    Distinct from every class above: those describe a choice the candidate
    made. Recording one of them for a turn that never ran would put a
    fabricated value in the stored ``agreement`` column and sort the hardest
    failure to the bottom of the report."""


class JudgeVerdict(StrEnum):
    """Adjudication of a divergence that already cleared the safety tier."""

    EQUIVALENT = "equivalent"
    CANDIDATE_BETTER = "candidate_better"
    CANDIDATE_WORSE = "candidate_worse"
    CANDIDATE_UNSAFE = "candidate_unsafe"
    NOT_JUDGED = "not_judged"
    JUDGE_FAILED = "judge_failed"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    """Process died mid-run. Set at startup, never by the run itself."""
    CANCELLED = "cancelled"


class Recommendation(StrEnum):
    SAFE_TO_SWITCH = "safe_to_switch"
    SWITCH_WITH_MONITORING = "switch_with_monitoring"
    DO_NOT_SWITCH = "do_not_switch"
    INCONCLUSIVE = "inconclusive"
    """Too few turns completed to say anything. Not a pass."""


@dataclass(frozen=True)
class ReplaySample:
    """One historic inbound turn, selected for replay.

    ``message_context`` is the persisted ``processed_context`` when present,
    which is the exact string production passed to the agent for this turn
    (body plus media transcription and OCR). Falling back to ``body`` only
    affects rows written before that column was populated.
    """

    seq: int
    timestamp: str
    message_context: str
    historic_reply: str = ""
    historic_tool_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation parsed out of a model's response blocks."""

    name: str
    arguments: dict[str, Any]


@dataclass
class ModelCallResult:
    """What one model returned for one replayed turn."""

    provider: str
    model: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    latency_ms: float = 0.0
    error: str = ""

    @property
    def acted(self) -> bool:
        """Whether the model chose to call at least one tool."""
        return bool(self.tool_calls)


@dataclass
class SafetyIssue:
    """A single safety finding with enough detail to act on it."""

    finding: SafetyFinding
    tool_name: str = ""
    detail: str = ""


@dataclass
class TurnComparison:
    """The full result for one replayed turn: both calls plus the verdict."""

    sample: ReplaySample
    baseline: ModelCallResult
    candidate: ModelCallResult
    agreement: AgreementClass
    safety_issues: list[SafetyIssue] = field(default_factory=list)
    judge_verdict: JudgeVerdict = JudgeVerdict.NOT_JUDGED
    judge_rationale: str = ""

    @property
    def is_blocking(self) -> bool:
        return bool(self.safety_issues) or self.judge_verdict is JudgeVerdict.CANDIDATE_UNSAFE

    @property
    def diverged(self) -> bool:
        return self.agreement is not AgreementClass.IDENTICAL
