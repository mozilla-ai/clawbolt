"""Tests for tool registry auto-discovery."""

from __future__ import annotations

import importlib
from unittest.mock import patch

from backend.app.agent.tools.registry import ensure_tool_modules_imported

EXPECTED_TOOL_MODULES: set[str] = {
    "backend.app.agent.tools.memory_tools",
    "backend.app.agent.tools.messaging_tools",
    "backend.app.agent.tools.heartbeat_tools",
    "backend.app.agent.tools.file_tools",
    "backend.app.agent.tools.quickbooks_tools",
    "backend.app.agent.tools.calendar_tools",
    "backend.app.agent.tools.workspace_tools",
    "backend.app.agent.tools.permission_tools",
}


def test_auto_discovery_finds_all_tool_modules() -> None:
    """ensure_tool_modules_imported discovers every *_tools module."""
    imported: list[str] = []
    original_import = importlib.import_module

    def tracking_import(name: str) -> object:
        imported.append(name)
        return original_import(name)

    with patch.object(importlib, "import_module", side_effect=tracking_import):
        ensure_tool_modules_imported()

    discovered = {m for m in imported if m.endswith("_tools")}
    assert discovered == EXPECTED_TOOL_MODULES


def test_auto_discovery_ignores_non_tool_modules() -> None:
    """Modules not ending with '_tools' (e.g. base, registry, names) are skipped."""
    imported: list[str] = []
    original_import = importlib.import_module

    def tracking_import(name: str) -> object:
        imported.append(name)
        return original_import(name)

    with patch.object(importlib, "import_module", side_effect=tracking_import):
        ensure_tool_modules_imported()

    non_tool = {
        m for m in imported if m.startswith("backend.app.agent.tools.") and not m.endswith("_tools")
    }
    assert non_tool == set(), f"Non-tool modules were imported: {non_tool}"
