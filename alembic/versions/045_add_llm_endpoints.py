"""Add ``llm_endpoints`` plus the per-side endpoint and effort an eval run records.

An endpoint is an operator-named destination: dialect, base URL, its own
credential, and explicit answers to the questions a provider name used to be
asked to answer on its own (cache markers, reasoning shape, whether a price
list applies). See ``services/llm_endpoints.py``.

Every added column is NOT NULL with an empty server default, which is also
what "not selected" means for each of them, so existing rows need no
backfill: a deployment that names no endpoint keeps the behavior it had.

Revision ID: 045
Revises: 044
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from backend.app.models import EncryptedString

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_endpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("dialect", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        # Envelope-encrypted like every other credential at rest. Empty means
        # "no key of its own", which leaves any-llm resolving the dialect's
        # environment variable.
        sa.Column("api_key", EncryptedString(), nullable=False, server_default=""),
        sa.Column("cache_control", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("reasoning", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("pricing", sa.String(length=16), nullable=False, server_default="auto"),
        sa.Column("notes", sa.String(length=256), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_endpoints_name"), "llm_endpoints", ["name"], unique=True)

    op.add_column(
        "subscriptions",
        sa.Column("llm_endpoint_override", sa.String(length=64), nullable=False, server_default=""),
    )

    for column in ("baseline_endpoint", "candidate_endpoint"):
        op.add_column(
            "llm_eval_runs",
            sa.Column(column, sa.String(length=64), nullable=False, server_default=""),
        )
    # Empty on historic rows is honest: those runs took whichever global
    # effort was in force at the time, which is no longer recoverable.
    for column in ("baseline_reasoning_effort", "candidate_reasoning_effort"):
        op.add_column(
            "llm_eval_runs",
            sa.Column(column, sa.String(length=16), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for column in (
        "candidate_reasoning_effort",
        "baseline_reasoning_effort",
        "candidate_endpoint",
        "baseline_endpoint",
    ):
        op.drop_column("llm_eval_runs", column)
    op.drop_column("subscriptions", "llm_endpoint_override")
    op.drop_index(op.f("ix_llm_endpoints_name"), table_name="llm_endpoints")
    op.drop_table("llm_endpoints")
