"""Tests for the auto -> always PERMISSIONS.json migration (alembic revision 014)."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture()
def migrate_file() -> Callable[[Path, str, str], bool]:
    """Import the _migrate_file helper from the alembic migration."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "migration_014",
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "014_rename_auto_to_always_permissions.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migration_014"] = mod
    spec.loader.exec_module(mod)
    return mod._migrate_file


def test_migrate_auto_to_always_in_tools(
    tmp_path: Path, migrate_file: Callable[[Path, str, str], bool]
) -> None:
    """Migration rewrites 'auto' -> 'always' in the tools section."""
    perm_file = tmp_path / "PERMISSIONS.json"
    data = {"version": 1, "tools": {"send_reply": "auto", "read_file": "auto"}, "resources": {}}
    perm_file.write_text(json.dumps(data))

    assert migrate_file(perm_file, "auto", "always") is True

    result = json.loads(perm_file.read_text())
    assert result["tools"]["send_reply"] == "always"
    assert result["tools"]["read_file"] == "always"


def test_migrate_auto_to_always_in_resources(
    tmp_path: Path, migrate_file: Callable[[Path, str, str], bool]
) -> None:
    """Migration rewrites 'auto' -> 'always' in the resources section."""
    perm_file = tmp_path / "PERMISSIONS.json"
    data = {
        "version": 1,
        "tools": {"web_fetch": "ask"},
        "resources": {"web_fetch": {"homedepot.com": "auto", "*.gov": "auto"}},
    }
    perm_file.write_text(json.dumps(data))

    assert migrate_file(perm_file, "auto", "always") is True

    result = json.loads(perm_file.read_text())
    assert result["resources"]["web_fetch"]["homedepot.com"] == "always"
    assert result["resources"]["web_fetch"]["*.gov"] == "always"
    assert result["tools"]["web_fetch"] == "ask"


def test_migrate_no_change_when_already_always(
    tmp_path: Path, migrate_file: Callable[[Path, str, str], bool]
) -> None:
    """Migration is a no-op when all values are already 'always'."""
    perm_file = tmp_path / "PERMISSIONS.json"
    data = {"version": 1, "tools": {"read_file": "always", "send_reply": "ask"}, "resources": {}}
    perm_file.write_text(json.dumps(data))

    assert migrate_file(perm_file, "auto", "always") is False


def test_migrate_preserves_ask_and_deny(
    tmp_path: Path, migrate_file: Callable[[Path, str, str], bool]
) -> None:
    """Migration does not touch 'ask' or 'deny' values."""
    perm_file = tmp_path / "PERMISSIONS.json"
    data = {
        "version": 1,
        "tools": {"send_reply": "ask", "blocked_tool": "deny", "read_file": "auto"},
        "resources": {},
    }
    perm_file.write_text(json.dumps(data))

    migrate_file(perm_file, "auto", "always")

    result = json.loads(perm_file.read_text())
    assert result["tools"]["send_reply"] == "ask"
    assert result["tools"]["blocked_tool"] == "deny"
    assert result["tools"]["read_file"] == "always"


def test_downgrade_always_to_auto(
    tmp_path: Path, migrate_file: Callable[[Path, str, str], bool]
) -> None:
    """Downgrade rewrites 'always' -> 'auto'."""
    perm_file = tmp_path / "PERMISSIONS.json"
    data = {"version": 1, "tools": {"read_file": "always"}, "resources": {}}
    perm_file.write_text(json.dumps(data))

    assert migrate_file(perm_file, "always", "auto") is True

    result = json.loads(perm_file.read_text())
    assert result["tools"]["read_file"] == "auto"
