"""Add data_sharing_consent + data_sharing_consent_at to users.

Privacy gate for content surfaced to admins (item 3 of clawbolt-premium
issue #325). Defaults to False so existing users are opted out — admins
only see a user's message bodies / memory / soul prompt for users who
have explicitly toggled this on. ``data_sharing_consent_at`` is set on
every change (opt-in AND opt-out) so consent history can be
reconstructed by joining against an audit trail if one exists.

The upgrade adds the boolean with ``server_default='false'`` so the
backfill runs at the database level (instant for any table size) and
the new application code can safely treat the column as NOT NULL on
the very first request post-migration.

Revision ID: 019
Revises: 018
Create Date: 2026-05-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: str = "018"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "data_sharing_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("data_sharing_consent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "data_sharing_consent_at")
    op.drop_column("users", "data_sharing_consent")
