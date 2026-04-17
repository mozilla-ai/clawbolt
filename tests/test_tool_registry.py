"""Tests for tool registry auto-discovery."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

import backend.app.agent.tools.registry as _reg
from backend.app.agent.tools.registry import ensure_tool_modules_imported


@pytest.fixture(autouse=True)
def _reset_import_guard() -> None:
    """Reset the once-only guard so each test triggers fresh discovery."""
    _reg._tool_modules_imported = False


EXPECTED_TOOL_MODULES: set[str] = {
    "backend.app.agent.tools.calculator_tools",
    "backend.app.agent.tools.memory_tools",
    "backend.app.agent.tools.messaging_tools",
    "backend.app.agent.tools.heartbeat_tools",
    "backend.app.agent.tools.file_tools",
    "backend.app.agent.tools.integration_tools",
    "backend.app.agent.tools.pricing_tools",
    "backend.app.agent.tools.quickbooks_tools",
    "backend.app.agent.tools.calendar_tools",
    "backend.app.agent.tools.companycam_tools",
    "backend.app.agent.tools.workspace_tools",
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
