"""Model-swap evaluation runs and their per-turn results."""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base
from backend.app.models.types import EncryptedString


class LLMEvalRun(Base):
    """One model-comparison run for one user, triggered from the admin console.

    A run replays the user's most recent turns through the incumbent model and
    a candidate model and records what each decided. It exists to answer
    "is it safe to move this user to that model" with evidence rather than
    with a vibe, and the row is kept afterwards so runs against different
    candidates stay comparable.

    ``summary_json`` holds the serialized aggregate (agreement buckets, safety
    counts, token and cost totals, warnings). It is denormalized on purpose:
    the per-turn rows are the evidence, and recomputing an aggregate from them
    would silently change historical verdicts whenever a threshold moves.

    Nothing here is charged to the user. Eval calls bypass the quota pipeline
    and are not written to ``llm_usage_logs``, so an analysis run never shows
    up as the user's own spend.
    """

    __tablename__ = "llm_eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The id the API and the report URL use. A run's report is a page an
    # operator bookmarks, revisits, and pastes to someone else, so its address
    # must not be a guessable row counter that also leaks how many runs exist.
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, nullable=False, default=lambda: str(_uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # ``SET NULL`` rather than CASCADE: deleting an admin must not delete the
    # evidence behind a switching decision they made.
    created_by_admin_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Each side records the endpoint it was sent to and the reasoning effort
    # it ran at, because neither is recoverable afterwards: the settings they
    # defaulted from are mutable, and a run whose effort is unknown cannot be
    # reproduced or compared against another run.
    baseline_endpoint: Mapped[str] = mapped_column(String(64), default="")
    baseline_provider: Mapped[str] = mapped_column(String(64), default="")
    baseline_model: Mapped[str] = mapped_column(String(128), default="")
    baseline_reasoning_effort: Mapped[str] = mapped_column(String(16), default="")
    candidate_endpoint: Mapped[str] = mapped_column(String(64), default="")
    candidate_provider: Mapped[str] = mapped_column(String(64), default="")
    candidate_model: Mapped[str] = mapped_column(String(128), default="")
    candidate_reasoning_effort: Mapped[str] = mapped_column(String(16), default="")
    judge_provider: Mapped[str] = mapped_column(String(64), default="")
    judge_model: Mapped[str] = mapped_column(String(128), default="")

    requested_samples: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    progress_completed: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    recommendation: Mapped[str] = mapped_column(String(32), default="")
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Touched by the worker as each turn lands. The startup sweep uses it to
    # tell a run abandoned by a dead process from one still advancing in
    # another: during a rolling deploy the new instance boots while the old
    # one is still draining, and a sweep with no liveness signal marks a
    # running evaluation interrupted underneath itself.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    turns: Mapped[list[LLMEvalTurnResult]] = relationship(
        "LLMEvalTurnResult",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class LLMEvalTurnResult(Base):
    """What both models decided for one replayed turn of one run.

    Every text column here is reconstructed from, or generated in response
    to, the user's real conversation, so all of them carry the same
    ``EncryptedString`` treatment as ``messages``. ``user_message`` is the
    user's own words; the tool-call payloads routinely embed customer names,
    addresses and phone numbers; and the judge rationale quotes both back.

    Token and latency columns stay plaintext: they are measurements, not
    content, and the report aggregates them on read.
    """

    __tablename__ = "llm_eval_turn_results"
    __table_args__ = (UniqueConstraint("run_id", "message_seq", name="uq_eval_turn_run_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("llm_eval_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    message_timestamp: Mapped[str] = mapped_column(String, default="")

    user_message: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="user_message"), default=""
    )
    historic_reply: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="historic_reply"), default=""
    )
    historic_tool_names: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="historic_tool_names"), default=""
    )

    baseline_text: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="baseline_text"), default=""
    )
    baseline_tool_calls: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="baseline_tool_calls"), default=""
    )
    baseline_stop_reason: Mapped[str] = mapped_column(String(64), default="")
    baseline_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    baseline_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    baseline_cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    baseline_cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    baseline_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_error: Mapped[str] = mapped_column(Text, default="")

    candidate_text: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="candidate_text"), default=""
    )
    candidate_tool_calls: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="candidate_tool_calls"), default=""
    )
    candidate_stop_reason: Mapped[str] = mapped_column(String(64), default="")
    candidate_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    candidate_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    candidate_cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    candidate_cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    candidate_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    candidate_error: Mapped[str] = mapped_column(Text, default="")

    agreement: Mapped[str] = mapped_column(String(48), default="")
    safety_issues: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="safety_issues"), default=""
    )
    judge_verdict: Mapped[str] = mapped_column(String(32), default="not_judged")
    judge_rationale: Mapped[str] = mapped_column(
        EncryptedString(table="llm_eval_turn_results", column="judge_rationale"), default=""
    )

    run: Mapped[LLMEvalRun] = relationship("LLMEvalRun", back_populates="turns", lazy="raise")
