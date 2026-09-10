"""Attribute usage rows to an endpoint, and record when their cost is unknown.

``llm_usage_logs.cost`` is priced from ``(provider, model)``. Behind a
gateway the provider names the dialect the request was written in, not
whoever billed the tokens, so a price-list hit on that pair is a coincidence.
``pricing_available`` marks those rows, so a SUM over ``cost`` can say it is a
lower bound instead of reading zero as free.

Existing rows default to ``pricing_available = true``. That is accurate about
endpoints, since none existed before this migration, but the column also
means "genai-prices knew this model", and historic rows for models it did not
know are backfilled ``true`` and will under-count in ``unpriced_calls``.
Backfilling those correctly would mean re-running the price lookup per row;
the counter is a completeness hint rather than an audit, so it is not worth
it.

Revision ID: 046
Revises: 045
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "046"
down_revision: str | None = "045"
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
