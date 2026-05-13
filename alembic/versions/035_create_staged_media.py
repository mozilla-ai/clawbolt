"""Create the ``staged_media`` table.

Persistence layer for inbound media bytes that the user has sent over a
messaging channel but the agent has not yet uploaded somewhere durable
(CompanyCam, Drive) or chosen to discard. The bytes themselves live on
the deployment's persistent volume under
``settings.media_staging_base_dir``; this table holds the metadata
(handle, original_url, mime, expiry, disk path) so a process restart
does not lose the agent's reference to photos a contractor sent earlier
in the week.

Replaces a prior in-process ``dict`` cache that lost everything on every
deploy (issue #1333).

Revision ID: 035
Revises: 034
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "035"
down_revision: str = "034"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "staged_media",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        ),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column(
            "mime_type", sa.String(), nullable=False, server_default="application/octet-stream"
        ),
        sa.Column("disk_path", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("handle", name="uq_staged_media_handle"),
        sa.UniqueConstraint("user_id", "original_url", name="uq_staged_media_user_original_url"),
    )
    # Lazy purges run ``DELETE WHERE expires_at < now()``; index keeps that
    # cheap as the table grows.
    op.create_index(
        "ix_staged_media_expires_at",
        "staged_media",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_staged_media_expires_at", table_name="staged_media")
    op.drop_table("staged_media")
