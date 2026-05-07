"""Drop the media_files manifest table.

Saved-file metadata now lives on the file itself in Google Drive
(``description``, ``appProperties.clawbolt_path``). The agent quotes
saved files by their storage path; there is no Clawbolt-side shadow
table any more.

Any remaining rows are stale local-storage references from the
pre-Drive era and have already lost their bytes; dropping them is the
only sensible recovery.

Revision ID: 032
Revises: 031
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "032"
down_revision: str = "031"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_table("media_files")


def downgrade() -> None:
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
