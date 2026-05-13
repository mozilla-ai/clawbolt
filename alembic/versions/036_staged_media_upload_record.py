"""Add upload-record columns to ``staged_media``.

Until this revision, ``media_staging.mark_uploaded`` wrote receipts to an
in-process dict that did not survive a worker restart. A same-handle
retry across a restart (deploy, OOM) bypassed the idempotency check in
``upload_to_storage`` and wrote a second Drive copy with the same
content. The bytes were durable; the receipt was not.

This revision moves the receipt onto the ``staged_media`` row itself so
its lifetime matches the bytes: same TTL, same uniqueness key, same
purge. All columns nullable; an unset upload-row means "no upload yet"
which is the correct semantics for existing rows.

See issue #1347 for the failure scenario and design discussion.

Revision ID: 036
Revises: 035
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "036"
down_revision: str = "035"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("staged_media", sa.Column("upload_service", sa.String(), nullable=True))
    op.add_column("staged_media", sa.Column("upload_external_id", sa.Text(), nullable=True))
    op.add_column("staged_media", sa.Column("upload_url", sa.Text(), nullable=True))
    op.add_column("staged_media", sa.Column("upload_target", sa.Text(), nullable=True))
    op.add_column("staged_media", sa.Column("upload_status", sa.String(), nullable=True))
    op.add_column(
        "staged_media",
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("staged_media", "uploaded_at")
    op.drop_column("staged_media", "upload_status")
    op.drop_column("staged_media", "upload_target")
    op.drop_column("staged_media", "upload_url")
    op.drop_column("staged_media", "upload_external_id")
    op.drop_column("staged_media", "upload_service")
