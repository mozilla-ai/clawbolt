"""Tests for batch plan approval in the agent tool execution pipeline."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    PermissionLevel,
    PlanStep,
    format_plan_message,
    get_approval_gate,
    get_approval_store,
)
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.session_db import get_session_store
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.bus import OutboundMessage
from backend.app.models import User
from tests.conftest import create_test_session
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    text: str


class _WriteParams(BaseModel):
    path: str
    content: str


async def _echo_tool(text: str) -> ToolResult:
    return ToolResult(content=f"echo: {text}")


async def _write_tool(path: str, content: str) -> ToolResult:
    return ToolResult(content=f"wrote: {path}")


async def _delete_tool(text: str) -> ToolResult:
    return ToolResult(content=f"deleted: {text}")


def _auto_tool(name: str = "reader") -> Tool:
    """Tool with no approval policy (AUTO by default)."""
    return Tool(
        name=name,
        description="Read-only tool",
        function=_echo_tool,
        params_model=_EchoParams,
    )


def _ask_tool(name: str = "writer", desc_prefix: str = "Write") -> Tool:
    """Tool with ASK approval policy."""
    return Tool(
        name=name,
        description="Mutating tool",
        function=_echo_tool,
        params_model=_EchoParams,
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            description_builder=lambda args, p=desc_prefix: f"{p} {args.get('text', '')}",
        ),
    )


def _deny_tool(name: str = "blocked") -> Tool:
    """Tool with DENY approval policy."""
    return Tool(
        name=name,
        description="Blocked tool",
        function=_echo_tool,
        params_model=_EchoParams,
        approval_policy=ApprovalPolicy(default_level=PermissionLevel.DENY),
    )


# ---------------------------------------------------------------------------
# format_plan_message
# ---------------------------------------------------------------------------


class TestFormatPlanMessage:
    def test_single_ask_no_auto(self) -> None:
        """Single ask step with no auto steps: simple prompt."""
        ask = [PlanStep("writer", "Write USER.md", PermissionLevel.ASK)]
        msg = format_plan_message("Plan:", [], ask)
        assert "Write USER.md" in msg
        assert "yes" in msg
        assert "always" in msg

    def test_single_ask_with_auto(self) -> None:
        """Single ask step with auto steps: natural language format."""
        auto = [PlanStep("reader", "Read config", PermissionLevel.AUTO)]
        ask = [PlanStep("writer", "Write USER.md", PermissionLevel.ASK)]
        msg = format_plan_message("Plan:", auto, ask)
        assert "read config" in msg.lower()
        assert "approval" in msg.lower()
        assert "write user.md" in msg.lower()
        assert "yes" in msg

    def test_multiple_ask_steps(self) -> None:
        """Multiple ask steps: lists items needing approval."""
        auto = [PlanStep("reader", "Read config", PermissionLevel.AUTO)]
        ask = [
            PlanStep("writer", "Write USER.md", PermissionLevel.ASK),
            PlanStep("sender", "Send message", PermissionLevel.ASK),
        ]
        msg = format_plan_message("Here's what I need to do:", auto, ask)
        assert "read config" in msg.lower()
        assert "approval" in msg.lower()
        assert "Write USER.md" in msg
        assert "Send message" in msg
        assert "yes" in msg

    def test_empty_ask_returns_empty(self) -> None:
        """No ask steps: returns empty string."""
        msg = format_plan_message("Plan:", [], [])
        assert msg == ""

    def test_multiple_auto_grouped(self) -> None:
        """Multiple auto steps are combined in a single sentence."""
        auto = [
            PlanStep("reader1", "Read file A", PermissionLevel.AUTO),
            PlanStep("reader2", "Read file B", PermissionLevel.AUTO),
        ]
        ask = [
            PlanStep("writer", "Write result", PermissionLevel.ASK),
            PlanStep("sender", "Send message", PermissionLevel.ASK),
        ]
        msg = format_plan_message("Plan:", auto, ask)
        # Auto steps combined: "I'll read file a, read file b."
        assert "read file a" in msg.lower()
        assert "read file b" in msg.lower()
        assert "approval" in msg.lower()


# ---------------------------------------------------------------------------
# Batch approval in agent
# ---------------------------------------------------------------------------


class TestBatchApproval:
    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_all_auto_no_plan(self, mock_amessages: object, test_user: User) -> None:
        """All AUTO tools execute without prompting."""
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "reader", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([_auto_tool()])
        response = await agent.process_message("read it")
        assert any(tc.name == "reader" and not tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_all_deny(self, mock_amessages: object, test_user: User) -> None:
        """All DENY tools return errors."""
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "blocked", "arguments": {"text": "x"}}]),
            make_text_response("Blocked!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([_deny_tool()])
        response = await agent.process_message("do it")
        assert any(tc.name == "blocked" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_mixed_plan_approved(self, mock_amessages: object, test_user: User) -> None:
        """Mixed AUTO+ASK tools: user approves plan, all execute."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "reader", "arguments": {"text": "config"}},
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([_auto_tool(), _ask_tool()])

        task = asyncio.create_task(_approve_soon())
        response = await agent.process_message("read then write")
        await task

        # Both tools should have executed
        assert any(tc.name == "reader" and not tc.is_error for tc in response.tool_calls)
        assert any(tc.name == "writer" and not tc.is_error for tc in response.tool_calls)

        # A plan message should have been sent
        plan_sent = False
        for call in mock_publish.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("msg")
            if isinstance(msg, OutboundMessage) and "approval" in msg.content.lower():
                plan_sent = True
        assert plan_sent

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_mixed_plan_denied_auto_still_executes(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Mixed plan denied: auto tools still execute, ask tools denied."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "reader", "arguments": {"text": "config"}},
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Partially done!"),
        ]

        gate = get_approval_gate()

        async def _deny_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.DENIED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([_auto_tool(), _ask_tool()])

        task = asyncio.create_task(_deny_soon())
        response = await agent.process_message("read then write")
        await task

        # Auto tool executed, ask tool denied
        assert any(tc.name == "reader" and not tc.is_error for tc in response.tool_calls)
        assert any(tc.name == "writer" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_persists_auto_for_all_ask_tools(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'always' on a batch plan persists AUTO for ALL ask tools."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "writer", "arguments": {"text": "data"}},
                    {"name": "sender", "arguments": {"text": "msg"}},
                ]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools(
            [
                _ask_tool("writer", "Write"),
                _ask_tool("sender", "Send"),
            ]
        )

        task = asyncio.create_task(_always_soon())
        await agent.process_message("write and send")
        await task

        # Both should now be AUTO in the store
        store = get_approval_store()
        assert store.check_permission(test_user.id, "writer") == PermissionLevel.AUTO
        assert store.check_permission(test_user.id, "sender") == PermissionLevel.AUTO

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_never_persists_deny_for_all_ask_tools(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'never' on a batch plan persists DENY for ALL ask tools."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "writer", "arguments": {"text": "data"}},
                    {"name": "sender", "arguments": {"text": "msg"}},
                ]
            ),
            make_text_response("Blocked!"),
        ]

        gate = get_approval_gate()

        async def _never_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.ALWAYS_DENY)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools(
            [
                _ask_tool("writer", "Write"),
                _ask_tool("sender", "Send"),
            ]
        )

        task = asyncio.create_task(_never_soon())
        await agent.process_message("write and send")
        await task

        store = get_approval_store()
        assert store.check_permission(test_user.id, "writer") == PermissionLevel.DENY
        assert store.check_permission(test_user.id, "sender") == PermissionLevel.DENY

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_timeout_denies_ask_tools(self, mock_amessages: object, test_user: User) -> None:
        """Timeout on plan approval denies ask tools, auto tools still execute."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "reader", "arguments": {"text": "config"}},
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Timed out!"),
        ]

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([_auto_tool(), _ask_tool()])

        # Use a very short timeout to trigger timeout quickly
        with patch("backend.app.agent.approval.settings.approval_timeout_seconds", 0.01):
            response = await agent.process_message("read then write")

        assert any(tc.name == "reader" and not tc.is_error for tc in response.tool_calls)
        assert any(tc.name == "writer" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_no_channel_denies_ask_tools(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """No publish_outbound (headless mode) denies ask tools."""
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Denied!"),
        ]

        # No publish_outbound or chat_id
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([_ask_tool()])
        response = await agent.process_message("write it")
        assert any(tc.name == "writer" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_stored_auto_skips_plan(self, mock_amessages: object, test_user: User) -> None:
        """Tools already set to AUTO in store skip the plan prompt."""
        mock_publish = AsyncMock()

        store = get_approval_store()
        store.set_permission(test_user.id, "writer", PermissionLevel.AUTO)

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Done!"),
        ]

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([_ask_tool()])
        response = await agent.process_message("write it")

        # Tool should execute without any plan prompt
        assert any(tc.name == "writer" and not tc.is_error for tc in response.tool_calls)
        # No plan message sent (only typing indicators)
        for call in mock_publish.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("msg")
            if isinstance(msg, OutboundMessage):
                assert "approval" not in msg.content.lower()

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_plan_prompt_not_double_wrapped(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """The approval prompt should not wrap the plan message with a second 'Reply:' line."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "writer", "arguments": {"text": "data"}}]),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([_ask_tool()])

        task = asyncio.create_task(_approve_soon())
        await agent.process_message("write it")
        await task

        # Find the approval prompt message
        approval_msgs = []
        for call in mock_publish.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("msg")
            if isinstance(msg, OutboundMessage) and "Reply yes or no" in msg.content:
                approval_msgs.append(msg.content)

        assert len(approval_msgs) == 1
        # "Reply yes or no" should appear exactly once (not double-wrapped)
        assert approval_msgs[0].count("Reply yes or no") == 1
        # Should not contain the _format_approval_message wrapper
        assert "wants to use the tool" not in approval_msgs[0]

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_persists_per_resource(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'always' with resource_extractor persists AUTO for the specific resource only."""
        mock_publish = AsyncMock()

        def _extractor(args: dict[str, object]) -> str | None:
            return str(args["text"]) if args.get("text") else None

        tool = Tool(
            name="fetcher",
            description="Fetch data",
            function=_echo_tool,
            params_model=_EchoParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extractor,
                description_builder=lambda args: f"Fetch {args.get('text', '')}",
            ),
        )

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "fetcher", "arguments": {"text": "invoices"}}]),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_always_soon())
        await agent.process_message("fetch invoices")
        await task

        # "invoices" resource should be AUTO
        store = get_approval_store()
        assert (
            store.check_permission(test_user.id, "fetcher", resource="invoices")
            == PermissionLevel.AUTO
        )
        # Different resource should still be ASK (the default)
        assert (
            store.check_permission(test_user.id, "fetcher", resource="customers")
            == PermissionLevel.ASK
        )

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_approval_prompt_persisted_to_session(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Approval prompt is stored in session history so it appears in the web UI.

        Regression test for #840: tool approval sent to iMessage channel
        did not show up in the web UI because the prompt was never
        persisted to the session message history.
        """
        mock_publish = AsyncMock()
        session_id = "test-approval-persist"
        create_test_session(test_user.id, session_id=session_id, channel="linq")

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [
                    {"name": "reader", "arguments": {"text": "config"}},
                    {"name": "writer", "arguments": {"text": "data"}},
                ]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(
            user=test_user,
            channel="linq",
            publish_outbound=mock_publish,
            chat_id="chat_1",
            session_id=session_id,
        )
        agent.register_tools([_auto_tool(), _ask_tool()])

        task = asyncio.create_task(_approve_soon())
        await agent.process_message("read then write")
        await task

        # The approval prompt should be stored in session history
        store = get_session_store(test_user.id)
        session = store.load_session(session_id)
        assert session is not None
        outbound_msgs = [m for m in session.messages if m.direction == "outbound"]
        assert any("approval" in m.body.lower() for m in outbound_msgs), (
            "Approval prompt not found in session history"
        )
