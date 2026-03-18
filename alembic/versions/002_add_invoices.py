"""add invoices

Revision ID: 002
Revises: 001
Create Date: 2026-03-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

_now = sa.func.now()

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.String(),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            index=True,
            nullable=True,
        ),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("total_amount", sa.Numeric(12, 2), server_default="0.0"),
        sa.Column("status", sa.String(), server_default="draft"),
        sa.Column("pdf_url", sa.String(), server_default=""),
        sa.Column("storage_path", sa.String(), server_default=""),
        sa.Column("due_date", sa.String(), nullable=True),
        sa.Column(
            "estimate_id",
            sa.String(),
            sa.ForeignKey("estimates.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("notes", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "invoice_line_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "invoice_id",
            sa.String(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("quantity", sa.Numeric(12, 2), server_default="1.0"),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0.0"),
        sa.Column("total", sa.Numeric(12, 2), server_default="0.0"),
    )


def downgrade() -> None:
    op.drop_table("invoice_line_items")
    op.drop_table("invoices")
