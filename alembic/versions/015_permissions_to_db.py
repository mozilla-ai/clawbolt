"""Move per-user permissions from PERMISSIONS.json files into the database.

Creates the ``user_permissions`` table and backfills it from any
``PERMISSIONS.json`` files under ``settings.data_dir``. Existing files
are left on disk so a downgrade can still recover them; the app stops
reading them once this migration lands.

Revision ID: 015
Revises: 014
Create Date: 2026-04-14
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import sqlalchemy as sa

from alembic import op
from backend.app.config import settings

revision: str = "015"
down_revision: str = "014"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    op.create_table(
        "user_permissions",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    _backfill_from_disk()


def downgrade() -> None:
    op.drop_table("user_permissions")


def _backfill_from_disk() -> None:
    """Copy each user's existing PERMISSIONS.json into the new table.

    Best-effort: skips unreadable files, malformed JSON, and any user dir
    that doesn't correspond to a known user row. The files stay on disk
    so the data isn't lost if something goes wrong.

    ``settings.data_dir`` defaults to ``"data/users"`` (relative), which
    resolves against the alembic process's CWD. Docker images run
    alembic from ``/app`` so it finds ``/app/data/users``. If you ever
    run migrations from a different CWD, the backfill silently finds
    nothing -- set an absolute ``DATA_DIR`` env var to avoid surprises.

    Uses Postgres-specific ``ON CONFLICT DO UPDATE``. The project uses
    Postgres for tests and production; if that ever changes, switch
    this to an explicit SELECT-then-INSERT/UPDATE.
    """
    data_root = Path(settings.data_dir)
    if not data_root.exists():
        logger.info("permissions migration: no data dir at %s, nothing to backfill", data_root)
        return

    conn = op.get_bind()
    user_ids = {row[0] for row in conn.execute(sa.text("SELECT id FROM users")).fetchall()}

    inserted = 0
    for user_dir in data_root.iterdir():
        if not user_dir.is_dir():
            continue
        if user_dir.name not in user_ids:
            continue
        perm_file = user_dir / "PERMISSIONS.json"
        if not perm_file.exists():
            continue
        try:
            raw = perm_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("permissions migration: skipping %s (%s)", perm_file, exc)
            continue
        if not isinstance(parsed, dict):
            continue
        payload = json.dumps(parsed)
        conn.execute(
            sa.text(
                "INSERT INTO user_permissions (user_id, data) VALUES (:uid, :data) "
                "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data"
            ),
            {"uid": user_dir.name, "data": payload},
        )
        inserted += 1

    logger.info("permissions migration: backfilled %d row(s) from disk", inserted)
