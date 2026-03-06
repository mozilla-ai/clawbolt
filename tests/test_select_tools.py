"""Tests for progressive tool disclosure via list_capabilities meta-tool."""

import pytest
from pydantic import BaseModel

from backend.app.agent.file_store import ContractorData
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import (
    ToolContext,
    ToolRegistry,
    create_list_capabilities_tool,
    ensure_tool_modules_imported,
)

# Ensure all tool modules self-register with the default registry.
ensure_tool_modules_imported()


class _EmptyParams(BaseModel):
    """Minimal stand-in so the params_model check passes."""


def _make_tool(name: str) -> Tool:
    """Create a trivial tool for testing."""

    async def noop() -> ToolResult:
        return ToolResult(content="ok")

    return Tool(name=name, description=f"test {name}", function=noop, params_model=_EmptyParams)


def _build_test_registry() -> ToolRegistry:
    """Build a registry with 3 core and 3 specialist factories."""
    registry = ToolRegistry()
    # Core factories
    registry.register("memory", lambda ctx: [_make_tool("save_fact"), _make_tool("recall_facts")])
    registry.register("messaging", lambda ctx: [_make_tool("send_reply")])
    registry.register("profile", lambda ctx: [_make_tool("view_profile")])
    # Specialist factories
    registry.register(
        "estimate",
        lambda ctx: [_make_tool("generate_estimate")],
        core=False,
        summary="Generate professional estimates and quotes with PDF output",
    )
    registry.register(
        "checklist",
        lambda ctx: [_make_tool("add_checklist_item"), _make_tool("list_checklist_items")],
        core=False,
        summary="Manage recurring reminders and task checklists",
    )
    registry.register(
        "file",
        lambda ctx: [_make_tool("upload_to_storage")],
        requires_storage=True,
        core=False,
        summary="Upload and organize files in cloud storage",
    )
    return registry


class TestCoreSpecialistClassification:
    """Factories are correctly classified as core or specialist."""

    def test_core_factory_names(self) -> None:
        registry = _build_test_registry()
        assert registry.core_factory_names == {"memory", "messaging", "profile"}

    def test_specialist_factory_names(self) -> None:
        registry = _build_test_registry()
        assert registry.specialist_factory_names == {"estimate", "checklist", "file"}

    def test_core_defaults_to_true(self) -> None:
        registry = ToolRegistry()
        registry.register("x", lambda ctx: [])
        assert registry.core_factory_names == {"x"}
        assert registry.specialist_factory_names == set()


class TestCreateCoreTools:
    """create_core_tools only returns tools from core factories."""

    def test_only_core_tools_returned(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        tools = registry.create_core_tools(ctx)
        names = {t.name for t in tools}
        assert names == {"save_fact", "recall_facts", "send_reply", "view_profile"}

    def test_specialist_tools_excluded(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        tools = registry.create_core_tools(ctx)
        names = {t.name for t in tools}
        assert "generate_estimate" not in names
        assert "add_checklist_item" not in names
        assert "upload_to_storage" not in names


class TestAvailableSpecialistSummaries:
    """get_available_specialist_summaries filters by dependency satisfaction."""

    def test_returns_all_specialists_when_deps_met(self) -> None:
        from unittest.mock import MagicMock

        registry = _build_test_registry()
        ctx = ToolContext(
            contractor=ContractorData(id=1),
            storage=MagicMock(),
        )
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "checklist" in summaries
        assert "file" in summaries

    def test_excludes_file_when_no_storage(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1), storage=None)
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "checklist" in summaries
        assert "file" not in summaries

    def test_excludes_core_factories(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "memory" not in summaries
        assert "messaging" not in summaries
        assert "profile" not in summaries


class TestListCapabilitiesTool:
    """The list_capabilities meta-tool returns correct information."""

    @pytest.mark.asyncio
    async def test_list_all_categories(self) -> None:
        summaries = {
            "estimate": "Generate estimates",
            "checklist": "Manage checklists",
        }
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category=None)
        assert "estimate" in result.content
        assert "checklist" in result.content
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_activate_valid_category(self) -> None:
        summaries = {"estimate": "Generate estimates"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category="estimate")
        assert "activated" in result.content.lower()
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_activate_unknown_category_returns_error(self) -> None:
        summaries = {"estimate": "Generate estimates"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category="nonexistent")
        assert result.is_error
        assert "estimate" in result.content  # hint about available categories

    @pytest.mark.asyncio
    async def test_no_specialists_available(self) -> None:
        tool = create_list_capabilities_tool({})
        result = await tool.function(category=None)
        assert "no additional" in result.content.lower()
        assert not result.is_error

    def test_tool_has_correct_name(self) -> None:
        tool = create_list_capabilities_tool({"x": "test"})
        assert tool.name == ToolName.LIST_CAPABILITIES

    def test_tool_has_params_model(self) -> None:
        tool = create_list_capabilities_tool({"x": "test"})
        assert tool.params_model is not None

    def test_tool_usage_hint_lists_categories(self) -> None:
        summaries = {"estimate": "x", "checklist": "y"}
        tool = create_list_capabilities_tool(summaries)
        assert "checklist" in tool.usage_hint
        assert "estimate" in tool.usage_hint


class TestDefaultRegistryCoreSpecialistSplit:
    """The default registry correctly classifies built-in factories."""

    def test_core_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        assert "memory" in core
        assert "messaging" in core
        assert "profile" in core

    def test_specialist_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        specialist = default_registry.specialist_factory_names
        assert "estimate" in specialist
        assert "checklist" in specialist
        assert "file" in specialist

    def test_no_overlap(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        specialist = default_registry.specialist_factory_names
        assert not core & specialist


class TestDynamicToolActivation:
    """The agent activates specialist tools when list_capabilities is called."""

    def test_activate_specialist_adds_tools(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        agent = ClawboltAgent(
            contractor=ContractorData(id=1),
            tool_context=ctx,
            registry=registry,
        )
        core_tools = registry.create_core_tools(ctx)
        agent.register_tools(core_tools)

        # Before activation, no estimate tools
        assert "generate_estimate" not in agent._tools_by_name

        # Simulate list_capabilities call
        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            )
        ]
        activated = agent._check_specialist_activations(calls)
        assert activated
        assert "generate_estimate" in agent._tools_by_name

    def test_activation_is_idempotent(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        agent = ClawboltAgent(
            contractor=ContractorData(id=1),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(registry.create_core_tools(ctx))

        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            )
        ]
        agent._check_specialist_activations(calls)
        tool_count_after_first = len(agent.tools)

        # Second activation should not add duplicate tools
        activated = agent._check_specialist_activations(calls)
        assert not activated
        assert len(agent.tools) == tool_count_after_first

    def test_non_list_capabilities_call_ignored(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        agent = ClawboltAgent(
            contractor=ContractorData(id=1),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(registry.create_core_tools(ctx))

        calls = [
            ToolCallRequest(id="call_1", name="save_fact", arguments={"key": "x", "value": "y"})
        ]
        activated = agent._check_specialist_activations(calls)
        assert not activated

    def test_unknown_category_not_activated(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(contractor=ContractorData(id=1))
        agent = ClawboltAgent(
            contractor=ContractorData(id=1),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(registry.create_core_tools(ctx))
        initial_count = len(agent.tools)

        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "nonexistent"},
            )
        ]
        activated = agent._check_specialist_activations(calls)
        assert not activated
        assert len(agent.tools) == initial_count

    def test_no_registry_returns_false(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        agent = ClawboltAgent(
            contractor=ContractorData(id=1),
        )
        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            )
        ]
        activated = agent._check_specialist_activations(calls)
        assert not activated
