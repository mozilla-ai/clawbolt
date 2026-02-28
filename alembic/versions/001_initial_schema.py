"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-02-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contractors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(255), server_default="", nullable=False),
        sa.Column("phone", sa.String(50), server_default="", nullable=False),
        sa.Column("trade", sa.String(255), server_default="", nullable=False),
        sa.Column("location", sa.String(255), server_default="", nullable=False),
        sa.Column("hourly_rate", sa.Float(), nullable=True),
        sa.Column("soul_text", sa.Text(), server_default="", nullable=False),
        sa.Column("business_hours", sa.String(255), server_default="", nullable=False),
        sa.Column("preferences_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(255), server_default="", nullable=False),
        sa.Column("phone", sa.String(50), server_default="", nullable=False),
        sa.Column("email", sa.String(255), server_default="", nullable=False),
        sa.Column("address", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("category", sa.String(50), server_default="general", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("twilio_sid", sa.String(255), server_default="", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("media_urls_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("processed_context", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "estimates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("total_amount", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("pdf_url", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "estimate_line_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "estimate_id", sa.Integer(), sa.ForeignKey("estimates.id"), index=True, nullable=False
        ),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("unit_price", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("total", sa.Float(), server_default="0.0", nullable=False),
    )

    op.create_table(
        "media_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=True),
        sa.Column(
            "contractor_id",
            sa.Integer(),
            sa.ForeignKey("contractors.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("original_url", sa.String(500), server_default="", nullable=False),
        sa.Column("mime_type", sa.String(100), server_default="", nullable=False),
        sa.Column("processed_text", sa.Text(), server_default="", nullable=False),
        sa.Column("storage_url", sa.String(500), server_default="", nullable=False),
        sa.Column("storage_path", sa.String(500), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("media_files")
    op.drop_table("estimate_line_items")
    op.drop_table("estimates")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("memories")
    op.drop_table("clients")
    op.drop_table("contractors")
