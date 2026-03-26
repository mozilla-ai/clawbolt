"""Add cache token tracking columns to llm_usage_logs.

Stores Anthropic prompt caching metrics (cache_creation_input_tokens and
cache_read_input_tokens) alongside existing token counts. Both columns
are nullable so existing rows and non-Anthropic providers remain valid.

Revision ID: 010
Revises: 009
Create Date: 2026-03-26
"""

import sqlalchemy as sa

from alembic import op

revision: str = "010"
down_revision: str = "009"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_logs",
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "llm_usage_logs",
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_usage_logs", "cache_read_input_tokens")
    op.drop_column("llm_usage_logs", "cache_creation_input_tokens")
