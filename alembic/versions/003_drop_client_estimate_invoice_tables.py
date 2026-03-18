"""drop client, estimate, and invoice tables

Revision ID: 003
Revises: 002
Create Date: 2026-03-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

_now = sa.func.now()

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop in dependency order: line items first, then parents, then clients
    op.drop_table("invoice_line_items")
    op.drop_table("invoices")
    op.drop_table("estimate_line_items")
    op.drop_table("estimates")
    op.drop_table("clients")


def downgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), server_default=""),
        sa.Column("email", sa.String(), server_default=""),
        sa.Column("address", sa.String(), server_default=""),
        sa.Column("notes", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.UniqueConstraint("user_id", "slug", name="uq_client_user_slug"),
    )

    op.create_table(
        "estimates",
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
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "estimate_line_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "estimate_id",
            sa.String(),
            sa.ForeignKey("estimates.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("quantity", sa.Numeric(12, 2), server_default="1.0"),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0.0"),
        sa.Column("total", sa.Numeric(12, 2), server_default="0.0"),
    )

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
