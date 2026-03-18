"""add llm_model and llm_provider to users

Revision ID: 003
Revises: 002
Create Date: 2026-03-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("llm_model", sa.String(), server_default="", nullable=False))
    op.add_column(
        "users", sa.Column("llm_provider", sa.String(), server_default="", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("users", "llm_provider")
    op.drop_column("users", "llm_model")
