"""Rename 'auto' to 'always' in PERMISSIONS.json files.

Walks the data directory and rewrites any PERMISSIONS.json that contains
the legacy "auto" permission level to use "always" instead.

Revision ID: 014
Revises: 013
Create Date: 2026-04-02
"""

import json
import logging
from pathlib import Path

from backend.app.config import settings

revision: str = "014"
down_revision: str = "013"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger(__name__)


def _migrate_file(path: Path, old: str, new: str) -> bool:
    """Rewrite permission values in a single PERMISSIONS.json. Returns True if changed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return False

    if not isinstance(data, dict):
        return False

    changed = False
    tools = data.get("tools")
    if isinstance(tools, dict):
        for key, val in tools.items():
            if val == old:
                tools[key] = new
                changed = True
    resources = data.get("resources")
    if isinstance(resources, dict):
        for _tool, res_map in resources.items():
            if isinstance(res_map, dict):
                for key, val in res_map.items():
                    if val == old:
                        res_map[key] = new
                        changed = True

    if changed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.rename(path)
    return changed


def upgrade() -> None:
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        return
    count = 0
    for perm_file in data_dir.glob("*/PERMISSIONS.json"):
        if _migrate_file(perm_file, "auto", "always"):
            count += 1
    if count:
        logger.info("Migrated %d PERMISSIONS.json files: 'auto' -> 'always'", count)


def downgrade() -> None:
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        return
    count = 0
    for perm_file in data_dir.glob("*/PERMISSIONS.json"):
        if _migrate_file(perm_file, "always", "auto"):
            count += 1
    if count:
        logger.info("Reverted %d PERMISSIONS.json files: 'always' -> 'auto'", count)
