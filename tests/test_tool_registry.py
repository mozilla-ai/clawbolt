"""Tests for tool registry auto-discovery."""

from __future__ import annotations

import importlib
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.agent.tools.registry as _reg
from backend.app.agent.tools.base import Tool, ToolTags
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)


async def _auth_ok(_ctx: ToolContext) -> str | None:
    return None


async def _auth_not_connected(_ctx: ToolContext) -> str | None:
    return "Not connected"


@pytest.fixture(autouse=True)
def _reset_import_guard() -> None:
    """Reset the once-only guard so each test triggers fresh discovery."""
    _reg._tool_modules_imported = False


EXPECTED_CORE_TOOL_MODULES: set[str] = {
    "backend.app.agent.tools.calculator_tools",
    "backend.app.agent.tools.memory_tools",
    "backend.app.agent.tools.messaging_tools",
    "backend.app.agent.tools.heartbeat_tools",
    "backend.app.agent.tools.file_tools",
    "backend.app.agent.tools.integration_tools",
    "backend.app.agent.tools.media_tools",
    "backend.app.agent.tools.workspace_tools",
}

EXPECTED_INTEGRATION_MODULES: set[str] = {
    "backend.app.integrations.appfolio_vendor.factory",
    "backend.app.integrations.calendar.factory",
    "backend.app.integrations.companycam.factory",
    "backend.app.integrations.quickbooks.factory",
    "backend.app.integrations.supplier_pricing.factory",
}


def test_auto_discovery_finds_all_tool_modules() -> None:
    """ensure_tool_modules_imported discovers every *_tools and integration factory module."""
    imported: list[str] = []
    original_import = importlib.import_module

    def tracking_import(name: str) -> object:
        imported.append(name)
        return original_import(name)

    with patch.object(importlib, "import_module", side_effect=tracking_import):
        ensure_tool_modules_imported()

    core = {m for m in imported if m.endswith("_tools")}
    assert core == EXPECTED_CORE_TOOL_MODULES

    integrations = {m for m in imported if m.endswith(".factory") and ".integrations." in m}
    assert integrations == EXPECTED_INTEGRATION_MODULES


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
async def test_create_ready_specialist_tools_skips_unauthenticated() -> None:
    """A specialist whose ``auth_check`` returns a reason string (user has
    not connected the integration yet) must NOT appear in the ready list.

    Regression for the prod calendar bug observed 2026-04-29: the LLM was
    forced to call ``list_capabilities('calendar')`` on every turn because
    specialist tools were never pre-activated. Pre-activation now happens
    at agent boot for ready specialists, but only those passing auth.
    """
    from backend.app.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()

    async def _build_calendar(_ctx: ToolContext) -> list[Tool]:
        return [cast(Tool, MagicMock(name="calendar_create_event"))]

    async def _build_qb(_ctx: ToolContext) -> list[Tool]:
        return [cast(Tool, MagicMock(name="qb_query"))]

    reg.register(
        "calendar",
        _build_calendar,
        core=False,
        summary="Calendar tools",
        auth_check=_auth_ok,  # connected
    )
    reg.register(
        "quickbooks",
        _build_qb,
        core=False,
        summary="QuickBooks tools",
        auth_check=_auth_not_connected,  # NOT connected
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user = MagicMock()
    ctx.storage = MagicMock()
    ctx.publish_outbound = AsyncMock()

    tools, names = await reg.create_ready_specialist_tools(ctx)
    assert names == {"calendar"}
    assert len(tools) == 1


@pytest.mark.asyncio()
async def test_create_ready_specialist_tools_returns_empty_when_none_ready() -> None:
    """No connected specialists -> empty tool list and empty name set.

    Important: the caller uses the returned name set to seed
    ``activated_specialists``. An empty set must not pre-activate anything.
    """
    from backend.app.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()

    async def _build(_ctx: ToolContext) -> list[Tool]:
        return [cast(Tool, MagicMock())]

    reg.register(
        "qb",
        _build,
        core=False,
        summary="QB",
        auth_check=_auth_not_connected,
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user = MagicMock()
    ctx.storage = MagicMock()
    ctx.publish_outbound = AsyncMock()

    tools, names = await reg.create_ready_specialist_tools(ctx)
    assert tools == []
    assert names == set()


@pytest.mark.asyncio()
async def test_create_ready_specialist_tools_respects_excluded_factories() -> None:
    """User-disabled tool groups must not be pre-activated even when connected."""
    from backend.app.agent.tools.registry import ToolRegistry

    reg = ToolRegistry()

    async def _build(_ctx: ToolContext) -> list[Tool]:
        return [cast(Tool, MagicMock())]

    reg.register(
        "calendar",
        _build,
        core=False,
        summary="Calendar",
        auth_check=_auth_ok,
    )

    ctx = MagicMock(spec=ToolContext)
    ctx.user = MagicMock()
    ctx.storage = MagicMock()
    ctx.publish_outbound = AsyncMock()

    tools, names = await reg.create_ready_specialist_tools(ctx, excluded_factories={"calendar"})
    assert tools == []
    assert names == set()


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


@pytest.mark.asyncio()
async def test_create_tools_auto_attaches_approval_policy_for_subtools() -> None:
    """Tools registered via SubToolInfo must end up with an ApprovalPolicy.

    Without one, ``_get_tool_permission`` in core.py short-circuits to ALWAYS
    and ignores the user's stored override. The registry compensates by
    auto-attaching a default policy to any SubToolInfo-registered tool that
    didn't bring its own. This guarantees that a user who escalates a tool
    from the dashboard (e.g. flipping ``companycam_search_projects`` from
    'always' to 'ask') is actually prompted at runtime.
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

    tools = await default_registry.create_tools(ctx)
    tool_by_name = {t.name: t for t in tools}

    missing: list[str] = []
    for factory in default_registry._factories.values():
        for st in factory.sub_tools:
            tool = tool_by_name.get(st.name)
            if tool is None:
                continue  # factory may have skipped (auth-gated, missing deps)
            if tool.approval_policy is None:
                missing.append(st.name)

    assert not missing, (
        "Every SubToolInfo-registered tool must have an approval_policy after "
        "create_tools so the runtime gate consults the user's stored "
        "permission overrides:\n" + "\n".join(missing)
    )


@pytest.mark.asyncio()
async def test_create_tools_uses_subtool_default_for_synthesized_policy() -> None:
    """The auto-attached policy carries the SubToolInfo's default level.

    Spot-check: ``companycam_search_projects`` ships with no explicit
    ``approval_policy`` and a SubToolInfo default of 'always'. The synthesized
    policy must reflect that, so users who haven't overridden the level still
    see the same auto-execution behavior they get today.
    """
    from backend.app.agent.approval import PermissionLevel

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

    # Build a synthetic tool with no policy and register a stub factory so
    # we don't depend on companycam being authenticated in the test env.
    from pydantic import BaseModel

    from backend.app.agent.tools.base import Tool, ToolResult
    from backend.app.agent.tools.registry import SubToolInfo, ToolRegistry

    async def _noop_fn(**_: object) -> ToolResult:
        return ToolResult(content="")

    class _Params(BaseModel):
        pass

    tool = Tool(
        name="bare_tool",
        description="bare",
        function=_noop_fn,
        params_model=_Params,
    )

    reg = ToolRegistry()
    reg.register(
        "bare_factory",
        lambda _ctx: [tool],
        sub_tools=[
            SubToolInfo(
                "bare_tool",
                "Look something up without side effects",
                default_permission="always",
            )
        ],
    )

    out = await reg.create_tools(ctx)
    assert len(out) == 1
    assert out[0].approval_policy is not None
    assert out[0].approval_policy.default_level == PermissionLevel.ALWAYS
    assert out[0].approval_policy.description_builder is not None
    assert (
        out[0].approval_policy.description_builder({}) == "Look something up without side effects"
    )


@pytest.mark.asyncio()
async def test_state_mutating_tools_have_concurrency_group() -> None:
    """Tools that mutate shared state must declare a concurrency_group.

    The agent runs tool calls concurrently within a single LLM turn. Tools
    flagged with ``MODIFIES_PROFILE`` (workspace document writes) or
    ``SENDS_REPLY`` (user-facing message stream) touch resources that other
    tools could touch in the same turn, so they must serialize via a
    concurrency group rather than risking lost updates or out-of-order
    output. This test enforces that invariant across every registered
    factory so a new mutating tool cannot ship without declaring a group.
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

    requires_group = {ToolTags.MODIFIES_PROFILE, ToolTags.SENDS_REPLY}
    missing: list[str] = []

    for factory_name, factory in default_registry._factories.items():
        try:
            import inspect

            result = factory.create(ctx)
            tools: list[Tool] = await result if inspect.isawaitable(result) else result  # type: ignore[assignment]
        except Exception:
            continue

        for tool in tools:
            if tool.tags & requires_group and tool.concurrency_group is None:
                tags = ", ".join(sorted(t.value for t in tool.tags & requires_group))
                missing.append(
                    f"{factory_name}/{tool.name}: tagged [{tags}] but concurrency_group is None"
                )

    assert not missing, (
        "Tools tagged MODIFIES_PROFILE or SENDS_REPLY must set a "
        "concurrency_group so concurrent tool calls in a single turn do "
        "not race on shared state:\n" + "\n".join(missing)
    )
