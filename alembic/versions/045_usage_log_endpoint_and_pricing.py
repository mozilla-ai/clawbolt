"""Attribute usage rows to an endpoint, and record when their cost is unknown.

``llm_usage_logs.cost`` is priced from ``(provider, model)``. Behind a
gateway the provider names the dialect the request was written in, not
whoever billed the tokens, so a price-list hit on that pair is a coincidence.
``pricing_available`` marks those rows, so a SUM over ``cost`` can say it is a
lower bound instead of reading zero as free.

Existing rows default to ``pricing_available = true``, which is accurate for
them: every row written before this migration went to a bare provider, since
endpoints did not exist.

Revision ID: 045
Revises: 044
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_logs",
        sa.Column("endpoint", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "llm_usage_logs",
        sa.Column("pricing_available", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("llm_usage_logs", "pricing_available")
    op.drop_column("llm_usage_logs", "endpoint")
