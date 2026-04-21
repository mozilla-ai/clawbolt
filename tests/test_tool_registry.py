"""Tests for tool registry auto-discovery."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.agent.tools.registry as _reg
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)


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
    "backend.app.agent.tools.media_tools",
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


@pytest.mark.asyncio()
async def test_ask_sub_tools_have_approval_policy() -> None:
    """Every tool with default_permission='ask' must have an ApprovalPolicy.

    Without this, the WebUI shows 'ask' but the execution pipeline in
    core.py treats the tool as 'always' (auto-execute without prompting).
    This test covers ALL registered factories, not just one integration.
    """
    ensure_tool_modules_imported()

    ctx = MagicMock(spec=ToolContext)
    ctx.user = MagicMock()
    ctx.user.id = "test-user"
    ctx.storage = MagicMock()
    ctx.publish_outbound = AsyncMock()
    ctx.channel = "test"
    ctx.to_address = ""
    ctx.downloaded_media = []
    ctx.turn_text = ""

    missing: list[str] = []

    for factory_name, factory in default_registry._factories.items():
        if not factory.sub_tools:
            continue

        ask_names = {st.name for st in factory.sub_tools if st.default_permission == "ask"}
        if not ask_names:
            continue

        # Build the tools. Some factories need auth (OAuth tokens etc.),
        # so we patch the service loading to avoid real network calls.
        # If the factory raises, skip it (auth-gated integrations that
        # can't build tools without credentials).
        try:
            import inspect

            from backend.app.agent.tools.base import Tool

            result = factory.create(ctx)
            tools: list[Tool] = await result if inspect.isawaitable(result) else result  # type: ignore[assignment]
        except Exception:
            continue

        tool_map = {t.name: t for t in tools}
        for name in sorted(ask_names):
            tool = tool_map.get(name)
            if tool is not None and tool.approval_policy is None:
                missing.append(
                    f"{factory_name}/{name}: default_permission='ask' but no approval_policy"
                )

    assert not missing, (
        "Tools with default_permission='ask' must have an ApprovalPolicy "
        "on the Tool object so the runtime enforces permissions:\n" + "\n".join(missing)
    )
