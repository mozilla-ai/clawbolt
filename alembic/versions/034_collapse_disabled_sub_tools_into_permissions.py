"""Collapse ``tool_configs.disabled_sub_tools`` into ``user_permissions``.

Sub-tool gating used to live in two stores: ``PERMISSIONS.json`` held
``"always"`` / ``"ask"`` and the JSON-encoded ``disabled_sub_tools``
column on ``tool_configs`` held names the agent should never see. The
two could disagree (one set ``"calendar_create_event": "ask"`` while
the other listed it as disabled), which is the dimension this
migration removes. Sub-tool preference now lives only in
``user_permissions`` with a new ``"never"`` level whose runtime
semantics match the old ``disabled_sub_tools``: the tool is filtered
out of the LLM schema entirely.

Upgrade path:

1. For every ``tool_configs`` row with a non-empty ``disabled_sub_tools``
   JSON array, merge each name into the user's ``user_permissions.data``
   under ``tools[<name>] = "never"``. Existing keys at other levels are
   overwritten (the user just asked for "never" via the dashboard or
   via this migration's source-of-truth swap); existing keys at
   ``"never"`` are unchanged.
2. Users with no ``user_permissions`` row get a fresh row written with
   just the migrated ``"never"`` entries; ``ApprovalStore.ensure_complete``
   will backfill defaults on next agent turn.
3. Drop the ``disabled_sub_tools`` column. The migration short-circuits
   when the column is already gone so re-running upgrade is a no-op.

Down-migration recreates the column empty. We do not try to reconstruct
the original payload from ``"never"`` entries because the post-upgrade
state is the canonical one; pre-first-release backwards compatibility
is not a constraint.

Revision ID: 034
Revises: 033
Create Date: 2026-05-11
"""

from __future__ import annotations

import json
import logging
from typing import cast

import sqlalchemy as sa

from alembic import op

revision: str = "034"
down_revision: str = "033"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col["name"] for col in inspector.get_columns("tool_configs")}
    if "disabled_sub_tools" not in columns:
        logger.info(
            "034 collapse_disabled_sub_tools: column already absent; treating upgrade as no-op."
        )
        return

    _migrate_disabled_sub_tools(bind)
    op.drop_column("tool_configs", "disabled_sub_tools")


def downgrade() -> None:
    op.add_column(
        "tool_configs",
        sa.Column("disabled_sub_tools", sa.Text(), server_default=""),
    )


def _migrate_disabled_sub_tools(bind: sa.engine.Connection) -> None:
    """Move every non-empty ``disabled_sub_tools`` payload into
    ``user_permissions.data.tools`` under permission level ``"never"``.

    Iterates per user so a malformed row for one user does not poison
    the migration for the rest. Each user gets at most two SQL
    statements: one SELECT against ``user_permissions`` and one INSERT
    or UPDATE.
    """
    rows = bind.execute(
        sa.text(
            "SELECT user_id, disabled_sub_tools "
            "FROM tool_configs "
            "WHERE disabled_sub_tools IS NOT NULL AND disabled_sub_tools <> ''"
        )
    ).fetchall()

    by_user: dict[str, set[str]] = {}
    for user_id, payload in rows:
        names = _parse_payload(payload, user_id=user_id)
        if not names:
            continue
        by_user.setdefault(user_id, set()).update(names)

    migrated_users = 0
    migrated_entries = 0
    for user_id, names in by_user.items():
        existing = bind.execute(
            sa.text("SELECT data FROM user_permissions WHERE user_id = :uid"),
            {"uid": user_id},
        ).scalar_one_or_none()

        if existing is None:
            data: dict[str, object] = {"version": 1, "tools": {}, "resources": {}}
        else:
            try:
                parsed = json.loads(existing)
            except (TypeError, ValueError):
                parsed = None
            if not isinstance(parsed, dict):
                data = {"version": 1, "tools": {}, "resources": {}}
            else:
                data = parsed
                if not isinstance(data.get("tools"), dict):
                    data["tools"] = {}
                if not isinstance(data.get("resources"), dict):
                    data["resources"] = {}

        tools_raw = data["tools"]
        if isinstance(tools_raw, dict):
            tools = cast("dict[str, object]", tools_raw)
        else:
            tools = {}
            data["tools"] = tools
        for name in sorted(names):
            tools[name] = "never"
            migrated_entries += 1

        payload = json.dumps(data)
        bind.execute(
            sa.text(
                "INSERT INTO user_permissions (user_id, data) "
                "VALUES (:uid, :data) "
                "ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data"
            ),
            {"uid": user_id, "data": payload},
        )
        migrated_users += 1

    logger.info(
        "034 collapse_disabled_sub_tools: migrated %d entries across %d users",
        migrated_entries,
        migrated_users,
    )


def _parse_payload(raw: str | None, *, user_id: str) -> list[str]:
    """Parse a ``disabled_sub_tools`` JSON column into a list of names.

    Returns an empty list on missing or malformed JSON; logs the
    user_id so the operator can investigate but does not abort the
    migration.
    """
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "034 collapse_disabled_sub_tools: skipping malformed JSON "
            "in tool_configs.disabled_sub_tools for user %s",
            user_id,
        )
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item:
            out.append(item)
    return out
