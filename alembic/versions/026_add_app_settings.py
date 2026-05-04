"""Add app_settings table for the DB-backed SettingsStore.

Replaces the legacy ``data/config.json`` flow. See
``backend.app.config_store`` for the read/write path. Secrets (the keys
in ``_SECRET_SETTINGS``) are envelope-encrypted before write; the
``is_secret`` column flips the decrypt path on read so a single ``value``
column carries both kinds of settings.

The table is intentionally tiny: settings are read at most once per
boot (and on admin updates) and we want zero ceremony around adding new
keys.

Revision ID: 026
Revises: 025
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "026"
down_revision: str = "025"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        # ``value`` is the raw setting. For secret keys the column holds
        # an envelope-encrypted ciphertext (see SettingsStore); for
        # non-secret keys it's the literal value. Empty string means
        # "configured to empty" (distinct from "no row" / "not configured").
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        # ``is_secret`` mirrors ``backend.app.config_store._SECRET_SETTINGS``
        # at the row level so a future allowlist change doesn't silently
        # try to plaintext-read a row written under the prior policy.
        sa.Column(
            "is_secret",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # NULL when applied by an unauthenticated bootstrap path (e.g.
        # the one-shot config.json import). ON DELETE SET NULL so user
        # deletion doesn't require touching settings rows.
        sa.Column(
            "updated_by_user_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
