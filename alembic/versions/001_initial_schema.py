"""initial schema

Revision ID: 001
Revises: None
Create Date: 2026-03-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

_now = sa.func.now()

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), unique=True, nullable=False),
        sa.Column("phone", sa.String(), server_default=""),
        sa.Column("timezone", sa.String(), server_default=""),
        sa.Column("preferred_channel", sa.String(), server_default="telegram"),
        sa.Column("channel_identifier", sa.String(), server_default=""),
        sa.Column("onboarding_complete", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("heartbeat_opt_in", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("heartbeat_frequency", sa.String(), server_default="30m"),
        sa.Column("folder_scheme", sa.String(), server_default="by_client"),
        sa.Column("soul_text", sa.Text(), server_default=""),
        sa.Column("user_text", sa.Text(), server_default=""),
        sa.Column("heartbeat_text", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "channel_routes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("channel_identifier", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.UniqueConstraint("channel", "channel_identifier", name="uq_channel_route"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(), unique=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("channel", sa.String(), server_default=""),
        sa.Column("last_compacted_seq", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("last_message_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), server_default=""),
        sa.Column("processed_context", sa.Text(), server_default=""),
        sa.Column("tool_interactions_json", sa.Text(), server_default=""),
        sa.Column("external_message_id", sa.String(), server_default=""),
        sa.Column("media_urls_json", sa.Text(), server_default=""),
        sa.Column("timestamp", sa.DateTime(), nullable=False, server_default=_now),
        sa.UniqueConstraint("session_id", "seq", name="uq_message_seq"),
    )

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
        sa.Column("phone", sa.String(), server_default=""),
        sa.Column("email", sa.String(), server_default=""),
        sa.Column("address", sa.Text(), server_default=""),
        sa.Column("notes", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
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
        "media_files",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("message_id", sa.String(), server_default=""),
        sa.Column("original_url", sa.Text(), server_default=""),
        sa.Column("mime_type", sa.String(), server_default=""),
        sa.Column("processed_text", sa.Text(), server_default=""),
        sa.Column("storage_url", sa.Text(), server_default=""),
        sa.Column("storage_path", sa.String(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "memory_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("memory_text", sa.Text(), server_default=""),
        sa.Column("history_text", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "heartbeat_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("schedule", sa.String(), server_default="30m"),
        sa.Column("active_hours", sa.String(), server_default=""),
        sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "heartbeat_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("external_id", sa.String(), unique=True, nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "llm_usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("provider", sa.String(), server_default=""),
        sa.Column("model", sa.String(), server_default=""),
        sa.Column("input_tokens", sa.Integer(), server_default="0"),
        sa.Column("output_tokens", sa.Integer(), server_default="0"),
        sa.Column("total_tokens", sa.Integer(), server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), server_default="0.0"),
        sa.Column("purpose", sa.String(), server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=_now),
    )

    op.create_table(
        "tool_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("category", sa.String(), server_default=""),
        sa.Column("domain_group", sa.String(), server_default=""),
        sa.Column("domain_group_order", sa.Integer(), server_default="0"),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true")),
        sa.UniqueConstraint("user_id", "name", name="uq_tool_config_user_name"),
    )


def downgrade() -> None:
    op.drop_table("tool_configs")
    op.drop_table("llm_usage_logs")
    op.drop_table("idempotency_keys")
    op.drop_table("heartbeat_logs")
    op.drop_table("heartbeat_items")
    op.drop_table("memory_documents")
    op.drop_table("media_files")
    op.drop_table("estimate_line_items")
    op.drop_table("estimates")
    op.drop_table("clients")
    op.drop_table("messages")
    op.drop_table("sessions")
    op.drop_table("channel_routes")
    op.drop_table("users")
