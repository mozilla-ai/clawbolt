"""Tests for progressive tool disclosure via list_capabilities meta-tool."""

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

    def test_returns_all_specialists_when_deps_met(self) -> None:
        from unittest.mock import MagicMock

        registry = _build_test_registry()
        ctx = ToolContext(
            user=User(id="1"),
            storage=MagicMock(),
        )
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "heartbeat" in summaries
        assert "file" in summaries

    def test_excludes_file_when_no_storage(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=None)
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "estimate" in summaries
        assert "heartbeat" in summaries
        assert "file" not in summaries

    def test_excludes_core_factories(self) -> None:
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        summaries = registry.get_available_specialist_summaries(ctx)
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
        summaries = {"estimate": "x", "heartbeat": "y"}
        tool = create_list_capabilities_tool(summaries)
        assert "heartbeat" in tool.usage_hint
        assert "estimate" in tool.usage_hint

    @pytest.mark.asyncio
    async def test_already_activated_category_returns_short_message(self) -> None:
        """Calling list_capabilities for an already-activated category returns a
        short 'already active' message instead of the full SKILL.md instructions."""
        summaries = {"estimate": "Generate estimates"}
        activated: set[str] = set()
        tool = create_list_capabilities_tool(summaries, activated_specialists=activated)

        # First call: should return the full activation message
        result = await tool.function(category="estimate")
        assert "activated" in result.content.lower()
        assert "no work has been done yet" in result.content.lower()

        # Mark as activated (normally done by the agent loop)
        activated.add("estimate")

        # Second call: should return a short message, not the full instructions
        result2 = await tool.function(category="estimate")
        assert "already active" in result2.content.lower()
        assert not result2.is_error
        # Should NOT contain the full activation message again
        assert len(result2.content) < len(result.content)

    @pytest.mark.asyncio
    async def test_activation_warns_against_hallucinating_completion(self) -> None:
        """The activation message must explicitly say that no work has been
        done yet. Without this, the LLM occasionally treats specialist
        activation as completion and replies 'I uploaded the photo' without
        ever calling the actual upload tool.
        """
        tool = create_list_capabilities_tool({"companycam": "Photo uploads"})
        result = await tool.function(category="companycam")
        lower = result.content.lower()
        assert "no work has been done yet" in lower
        assert "call the specific tool" in lower

    @pytest.mark.asyncio
    async def test_already_active_warns_against_hallucinating_completion(self) -> None:
        """The 'already active' branch also has to discourage hallucinated
        completion, since the LLM may loop back to it."""
        summaries = {"companycam": "Photo uploads"}
        activated: set[str] = {"companycam"}
        tool = create_list_capabilities_tool(summaries, activated_specialists=activated)
        result = await tool.function(category="companycam")
        lower = result.content.lower()
        assert "no action has been performed" in lower
        assert "call the specific tool" in lower


class TestDefaultRegistryCoreSpecialistSplit:
    """The default registry correctly classifies built-in factories."""

    def test_core_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        assert "messaging" in core
        assert "workspace" in core
        assert "heartbeat" in core
        assert "file" in core

    def test_specialist_factories(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        specialist = default_registry.specialist_factory_names
        assert "quickbooks" in specialist
        assert "calendar" in specialist
        assert "heartbeat" not in specialist
        assert "file" not in specialist

    def test_no_overlap(self) -> None:
        from backend.app.agent.tools.registry import default_registry

        core = default_registry.core_factory_names
        specialist = default_registry.specialist_factory_names
        assert not core & specialist


class TestDynamicToolActivation:
    """The agent activates specialist tools when list_capabilities is called."""

    @pytest.mark.asyncio()
    async def test_activate_specialist_adds_tools(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        agent = ClawboltAgent(
            user=User(id="1"),
            tool_context=ctx,
            registry=registry,
        )
        core_tools = await registry.create_core_tools(ctx)
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
        activated = await agent._check_specialist_activations(calls)
        assert activated
        assert "generate_estimate" in agent._tools_by_name

    @pytest.mark.asyncio()
    async def test_activation_is_idempotent(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        agent = ClawboltAgent(
            user=User(id="1"),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(await registry.create_core_tools(ctx))

        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            )
        ]
        await agent._check_specialist_activations(calls)
        tool_count_after_first = len(agent.tools)

        # Second activation should not add duplicate tools
        activated = await agent._check_specialist_activations(calls)
        assert not activated
        assert len(agent.tools) == tool_count_after_first

    @pytest.mark.asyncio()
    async def test_non_list_capabilities_call_ignored(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        agent = ClawboltAgent(
            user=User(id="1"),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(await registry.create_core_tools(ctx))

        calls = [
            ToolCallRequest(id="call_1", name="send_media_reply", arguments={"message": "hello"})
        ]
        activated = await agent._check_specialist_activations(calls)
        assert not activated

    @pytest.mark.asyncio()
    async def test_unknown_category_not_activated(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        agent = ClawboltAgent(
            user=User(id="1"),
            tool_context=ctx,
            registry=registry,
        )
        agent.register_tools(await registry.create_core_tools(ctx))
        initial_count = len(agent.tools)

        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "nonexistent"},
            )
        ]
        activated = await agent._check_specialist_activations(calls)
        assert not activated
        assert len(agent.tools) == initial_count

    @pytest.mark.asyncio()
    async def test_no_registry_returns_false(self) -> None:
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.messages import ToolCallRequest

        agent = ClawboltAgent(
            user=User(id="1"),
        )
        calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            )
        ]
        activated = await agent._check_specialist_activations(calls)
        assert not activated

    @pytest.mark.asyncio
    async def test_specialist_tool_available_in_same_round_as_activation(self) -> None:
        """Regression: LLM calls list_capabilities + specialist tool in the same round.

        Previously, _check_specialist_activations ran AFTER _execute_tool_round,
        so the specialist tool would fail as "unknown tool" even though
        list_capabilities was called in the same batch. This caused the LLM
        to conclude the integration was broken (e.g. "QuickBooks connection
        dropped") when it was actually connected.

        The agent loop now pre-activates specialists before execution, while
        temporarily hiding them from the shared _activated_specialists set so
        list_capabilities still returns the full activation message with
        SKILL.md instructions.
        """
        from backend.app.agent.core import ClawboltAgent
        from backend.app.agent.llm_parsing import ParsedToolCall
        from backend.app.agent.messages import ToolCallRequest

        activated_set: set[str] = set()
        registry = _build_test_registry()
        ctx = ToolContext(user=User(id="1"))
        agent = ClawboltAgent(
            user=User(id="1"),
            tool_context=ctx,
            registry=registry,
            activated_specialists=activated_set,
        )

        specialist_summaries = registry.get_available_specialist_summaries(ctx)
        list_cap_tool = create_list_capabilities_tool(
            specialist_summaries,
            activated_specialists=activated_set,
        )
        core_tools = await registry.create_core_tools(ctx)
        core_tools.append(list_cap_tool)
        agent.register_tools(core_tools)

        # Simulate LLM calling list_capabilities AND a specialist tool in one round
        parsed_calls = [
            ToolCallRequest(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            ),
            ToolCallRequest(
                id="call_2",
                name="generate_estimate",
                arguments={},
            ),
        ]
        parsed_raw = [
            ParsedToolCall(
                id="call_1",
                name=ToolName.LIST_CAPABILITIES,
                arguments={"category": "estimate"},
            ),
            ParsedToolCall(
                id="call_2",
                name="generate_estimate",
                arguments={},
            ),
        ]

        # Replicate the agent loop's activation pattern: pre-activate, then
        # temporarily hide from the shared set during execution.
        pre_activated = set(agent._activated_specialists)
        await agent._check_specialist_activations(parsed_calls)
        newly_activated = agent._activated_specialists - pre_activated
        agent._activated_specialists -= newly_activated

        # Execute the tool round
        results = await agent._execute_tool_round(parsed_calls, parsed_raw, [], [], [])

        # Restore the activated set (as the agent loop does)
        agent._activated_specialists |= newly_activated

        # The specialist tool should NOT be an error
        estimate_result = next(r for r in results if r.tool_call_id == "call_2")
        assert not estimate_result.is_error, (
            f"Specialist tool failed despite activation: {estimate_result.content}"
        )

        # list_capabilities should return the full activation message (not
        # the "already active" short-circuit) since the set was hidden
        cap_result = next(r for r in results if r.tool_call_id == "call_1")
        assert "activated" in cap_result.content.lower()
        assert "already active" not in cap_result.content.lower()

        # After restoration, the category is in the activated set
        assert "estimate" in agent._activated_specialists
