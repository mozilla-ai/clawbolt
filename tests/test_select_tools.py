"""Tests for the list_capabilities meta-tool and core/specialist registry split.

list_capabilities is a discovery + guidance lookup, not an activation
mechanism: tools for authenticated integrations are loaded on the schema
from turn 1 (see ``create_ready_specialist_tools``).
"""

import pytest
from pydantic import BaseModel

from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import (
    ToolContext,
    ToolRegistry,
    create_list_capabilities_tool,
    ensure_tool_modules_imported,
)
from backend.app.models import User

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
    registry.register("messaging", lambda ctx: [_make_tool("send_media_reply")])
    registry.register("workspace", lambda ctx: [_make_tool("read_file"), _make_tool("write_file")])
    # Specialist factories
    registry.register(
        "estimate",
        lambda ctx: [_make_tool("generate_estimate")],
        core=False,
        summary="Generate professional estimates and quotes with PDF output",
    )
    registry.register(
        "heartbeat",
        lambda ctx: [_make_tool("get_heartbeat"), _make_tool("update_heartbeat")],
        core=False,
        summary="Manage recurring reminders and task heartbeats",
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
        assert registry.core_factory_names == {"messaging", "workspace"}

    def test_specialist_factory_names(self) -> None:
        registry = _build_test_registry()
        assert registry.specialist_factory_names == {"estimate", "heartbeat", "file"}

    def test_core_defaults_to_true(self) -> None:
        registry = ToolRegistry()
        registry.register("x", lambda ctx: [])
        assert registry.core_factory_names == {"x"}
        assert registry.specialist_factory_names == set()


class TestCreateCoreTools:
    """create_core_tools only returns tools from core factories."""

    @pytest.mark.asyncio()
    async def test_only_core_tools_returned(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        tools = await registry.create_core_tools(ctx)
        names = {t.name for t in tools}
        assert names == {"send_media_reply", "read_file", "write_file"}

    @pytest.mark.asyncio()
    async def test_specialist_tools_excluded(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        tools = await registry.create_core_tools(ctx)
        names = {t.name for t in tools}
        assert "generate_estimate" not in names
        assert "get_heartbeat" not in names
        assert "upload_to_storage" not in names


class TestAvailableSpecialistSummaries:
    """get_available_specialist_summaries filters by dependency satisfaction."""

    @pytest.mark.asyncio()
    async def test_returns_all_specialists_when_deps_met(self) -> None:
        from unittest.mock import MagicMock

        registry = _build_test_registry()
        ctx = ToolContext(
            user=User(id="1"),
            storage=MagicMock(),
        )
        summaries = await registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "heartbeat" in summaries
        assert "file" in summaries

    @pytest.mark.asyncio()
    async def test_excludes_file_when_no_storage(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=None)
        summaries = await registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "heartbeat" in summaries
        assert "file" not in summaries

    @pytest.mark.asyncio()
    async def test_excludes_core_factories(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        summaries = await registry.get_available_specialist_summaries(ctx)
        assert "messaging" not in summaries
        assert "workspace" not in summaries


class TestListCapabilitiesTool:
    """The list_capabilities meta-tool returns correct information."""

    @pytest.mark.asyncio
    async def test_list_all_categories(self) -> None:
        summaries = {
            "estimate": "Generate estimates",
            "heartbeat": "Manage heartbeats",
        }
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category=None)
        assert "estimate" in result.content
        assert "heartbeat" in result.content
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_lookup_known_category_returns_guidance(self) -> None:
        summaries = {"estimate": "Generate estimates"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category="estimate")
        assert "already loaded" in result.content.lower()
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
        summaries = {"estimate": "x", "heartbeat": "y"}
        tool = create_list_capabilities_tool(summaries)
        assert "heartbeat" in tool.usage_hint
        assert "estimate" in tool.usage_hint

    @pytest.mark.asyncio
    async def test_lookup_directs_llm_to_call_tool(self) -> None:
        """Looking up a category must explicitly direct the LLM to call the
        specific tool, and warn against claiming completion before the
        tool has run. Without this, the LLM occasionally treats the
        lookup as completion and replies 'I uploaded the photo' without
        ever calling the actual upload tool.
        """
        tool = create_list_capabilities_tool({"companycam": "Photo uploads"})
        result = await tool.function(category="companycam")
        lower = result.content.lower()
        assert "already loaded" in lower
        assert "call the specific tool" in lower
        assert "do not tell the user the action is complete" in lower


class TestDefaultRegistryCoreSpecialistSplit:
    """The default registry correctly classifies built-in factories."""

    def test_core_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        assert "messaging" in core
        assert "workspace" in core
        assert "heartbeat" in core

    def test_specialist_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        specialist = default_registry.specialist_factory_names
        assert "quickbooks" in specialist
        assert "calendar" in specialist
        assert "file" in specialist
        assert "heartbeat" not in specialist

    def test_no_overlap(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        specialist = default_registry.specialist_factory_names
        assert not core & specialist
