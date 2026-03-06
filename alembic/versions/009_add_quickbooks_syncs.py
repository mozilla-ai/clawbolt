"""Add quickbooks_syncs table

Revision ID: 009
Revises: 008
Create Date: 2026-03-03

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quickbooks_syncs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contractor_id", sa.Integer(), nullable=False),
        sa.Column("estimate_id", sa.Integer(), nullable=True),
        sa.Column("qb_entity_type", sa.String(50), nullable=False, server_default="invoice"),
        sa.Column("qb_entity_id", sa.String(255), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["contractor_id"], ["contractors.id"]),
        sa.ForeignKeyConstraint(["estimate_id"], ["estimates.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_quickbooks_syncs_contractor_id"), "quickbooks_syncs", ["contractor_id"]
    )
    op.create_index(op.f("ix_quickbooks_syncs_qb_entity_id"), "quickbooks_syncs", ["qb_entity_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_quickbooks_syncs_qb_entity_id"), table_name="quickbooks_syncs")
    op.drop_index(op.f("ix_quickbooks_syncs_contractor_id"), table_name="quickbooks_syncs")
    op.drop_table("quickbooks_syncs")
