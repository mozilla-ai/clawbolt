"""Add ``llm_eval_runs`` and ``llm_eval_turn_results``.

Backing store for the admin model-swap evaluator: an operator picks a user
and a candidate model, the evaluator replays that user's recent turns through
both the incumbent and the candidate, and these tables hold the run metadata
and the per-turn evidence behind the resulting recommendation.

Text columns on the turn table are envelope-encrypted at the application
layer (``EncryptedString``), so they are plain ``TEXT`` here and carry
ciphertext at rest. Token counts and latencies stay plaintext: they are
measurements, not user content.

Revision ID: 042
Revises: 041
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_eval_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("created_by_admin_id", sa.String(), nullable=True),
        sa.Column("baseline_provider", sa.String(length=64), server_default="", nullable=False),
        sa.Column("baseline_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("candidate_provider", sa.String(length=64), server_default="", nullable=False),
        sa.Column("candidate_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("judge_provider", sa.String(length=64), server_default="", nullable=False),
        sa.Column("judge_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("requested_samples", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("progress_completed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("progress_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("recommendation", sa.String(length=32), server_default="", nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_eval_runs_user_id", "llm_eval_runs", ["user_id"])
    op.create_index("ix_llm_eval_runs_status", "llm_eval_runs", ["status"])
    op.create_index("ix_llm_eval_runs_created_at", "llm_eval_runs", ["created_at"])

    op.create_table(
        "llm_eval_turn_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("message_seq", sa.Integer(), nullable=False),
        sa.Column("message_timestamp", sa.String(), server_default="", nullable=False),
        sa.Column("user_message", sa.Text(), server_default="", nullable=False),
        sa.Column("historic_reply", sa.Text(), server_default="", nullable=False),
        sa.Column("historic_tool_names", sa.Text(), server_default="", nullable=False),
        sa.Column("baseline_text", sa.Text(), server_default="", nullable=False),
        sa.Column("baseline_tool_calls", sa.Text(), server_default="", nullable=False),
        sa.Column("baseline_stop_reason", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "baseline_input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "baseline_output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "baseline_cache_read_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "baseline_cache_creation_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "baseline_latency_ms",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("baseline_error", sa.Text(), server_default="", nullable=False),
        sa.Column("candidate_text", sa.Text(), server_default="", nullable=False),
        sa.Column("candidate_tool_calls", sa.Text(), server_default="", nullable=False),
        sa.Column("candidate_stop_reason", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "candidate_input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "candidate_output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "candidate_cache_read_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "candidate_cache_creation_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "candidate_latency_ms",
            sa.Float(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("candidate_error", sa.Text(), server_default="", nullable=False),
        sa.Column("agreement", sa.String(length=48), server_default="", nullable=False),
        sa.Column("safety_issues", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "judge_verdict", sa.String(length=32), server_default="not_judged", nullable=False
        ),
        sa.Column("judge_rationale", sa.Text(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["llm_eval_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "message_seq", name="uq_eval_turn_run_seq"),
    )
    op.create_index("ix_llm_eval_turn_results_run_id", "llm_eval_turn_results", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_eval_turn_results_run_id", table_name="llm_eval_turn_results")
    op.drop_table("llm_eval_turn_results")
    op.drop_index("ix_llm_eval_runs_created_at", table_name="llm_eval_runs")
    op.drop_index("ix_llm_eval_runs_status", table_name="llm_eval_runs")
    op.drop_index("ix_llm_eval_runs_user_id", table_name="llm_eval_runs")
    op.drop_table("llm_eval_runs")
