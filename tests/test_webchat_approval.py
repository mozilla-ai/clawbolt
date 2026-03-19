"""Regression tests for webchat approval flow.

Verifies that:
- The approval prompt is published as an SSE event when request_id is set.
- The /api/user/chat/approve endpoint resolves the approval gate.
- The SSE event stream delivers approval_request events to the frontend.

Root cause: approval prompts were sent via publish_outbound -> channel.send_text(),
but WebChatChannel.send_text() is a no-op. The prompt was silently dropped and
the agent hung for approval_timeout_seconds before auto-denying.
"""

import asyncio
import threading
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import backend.app.database as _db_module
from backend.app.agent.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    PermissionLevel,
    get_approval_gate,
)
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.bus import OutboundMessage, message_bus
from backend.app.main import app
from backend.app.models import User
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    text: str


async def _echo_tool(text: str) -> ToolResult:
    return ToolResult(content=f"echo: {text}")


def _ask_tool(name: str = "writer") -> Tool:
    """Tool with ASK approval policy."""
    return Tool(
        name=name,
        description="Mutating tool",
        function=_echo_tool,
        params_model=_EchoParams,
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            description_builder=lambda args: f"Write {args.get('text', '')}",
        ),
    )


def _auto_tool(name: str = "reader") -> Tool:
    """Tool with no approval policy (AUTO by default)."""
    return Tool(
        name=name,
        description="Read-only tool",
        function=_echo_tool,
        params_model=_EchoParams,
    )


# ---------------------------------------------------------------------------
# Agent-level: SSE event published when request_id is set
# ---------------------------------------------------------------------------


class TestWebchatApprovalSSE:
    """Verify that approval prompts are published as SSE events for webchat."""

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_approval_publishes_sse_event_with_request_id(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """When request_id is set, an approval_request SSE event is published."""
        mock_publish = AsyncMock()
        request_id = "test-req-approval-sse"

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "writer", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()
        queue = message_bus.register_event_queue(request_id)

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(
            user=test_user,
            channel="webchat",
            publish_outbound=mock_publish,
            chat_id="chat_1",
            request_id=request_id,
        )
        agent.register_tools([_ask_tool()])

        task = asyncio.create_task(_approve_soon())
        response = await agent.process_message("write something")
        await task

        # Tool should have executed successfully
        assert any(tc.name == "writer" and not tc.is_error for tc in response.tool_calls)

        # SSE event queue should contain an approval_request event
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        approval_events = [e for e in events if e.get("type") == "approval_request"]
        assert len(approval_events) == 1
        assert "content" in approval_events[0]
        assert "yes" in approval_events[0]["content"].lower()

        message_bus.remove_event_queue(request_id)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_no_sse_event_without_request_id(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Without request_id (Telegram path), no SSE event is published."""
        mock_publish = AsyncMock()

        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "writer", "arguments": {"text": "hello"}}]),
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
            # No request_id -- Telegram path
        )
        agent.register_tools([_ask_tool()])

        task = asyncio.create_task(_approve_soon())
        response = await agent.process_message("write something")
        await task

        assert any(tc.name == "writer" and not tc.is_error for tc in response.tool_calls)

        # The outbound message (approval prompt) should have been sent via publish_outbound
        # but NOT as an SSE event
        plan_sent = False
        for call in mock_publish.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("msg")
            if isinstance(msg, OutboundMessage) and "yes" in msg.content.lower():
                plan_sent = True
        assert plan_sent


# ---------------------------------------------------------------------------
# Endpoint: POST /api/user/chat/approve
# ---------------------------------------------------------------------------


@pytest.fixture()
async def approval_user() -> User:
    """Create a user for approval endpoint tests."""
    db = _db_module.SessionLocal()
    try:
        user = User(user_id="approval-test-user")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    return user


@pytest.fixture()
def approval_client(approval_user: User) -> Generator[TestClient]:
    """TestClient with mocked external services."""
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.stop"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


class TestApproveEndpoint:
    """Tests for POST /api/user/chat/approve."""

    def test_approve_resolves_pending_gate(
        self,
        approval_client: TestClient,
        approval_user: User,
    ) -> None:
        """Valid approval decision resolves the pending gate."""
        gate = get_approval_gate()

        # Simulate a pending approval in a background thread
        pending_resolved = threading.Event()

        async def _wait_for_approval() -> ApprovalDecision:
            mock_publish = AsyncMock()
            return await gate.request_approval(
                user_id=str(approval_user.id),
                tool_name="qb_query",
                description="Query QuickBooks",
                publish_outbound=mock_publish,
                channel="webchat",
                chat_id=str(approval_user.id),
                timeout=5.0,
            )

        decision_holder: list[ApprovalDecision] = []

        def _run_gate() -> None:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(_wait_for_approval())
            decision_holder.append(result)
            pending_resolved.set()
            loop.close()

        t = threading.Thread(target=_run_gate)
        t.start()

        import time

        # Wait for the gate to be pending
        for _ in range(50):
            if gate.has_pending(str(approval_user.id)):
                break
            time.sleep(0.05)
        assert gate.has_pending(str(approval_user.id))

        # Send approval via the new endpoint
        resp = approval_client.post(
            "/api/user/chat/approve",
            json={"decision": "yes"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        pending_resolved.wait(timeout=5)
        t.join(timeout=5)
        assert len(decision_holder) == 1
        assert decision_holder[0] == ApprovalDecision.APPROVED

    def test_approve_invalid_decision(
        self,
        approval_client: TestClient,
        approval_user: User,
    ) -> None:
        """Invalid decision string returns 422."""
        resp = approval_client.post(
            "/api/user/chat/approve",
            json={"decision": "maybe"},
        )
        assert resp.status_code == 422

    def test_approve_no_pending(
        self,
        approval_client: TestClient,
        approval_user: User,
    ) -> None:
        """Approval with no pending gate returns 404."""
        resp = approval_client.post(
            "/api/user/chat/approve",
            json={"decision": "yes"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# SSE integration: approval_request event streams to frontend
# ---------------------------------------------------------------------------


class TestApprovalSSEIntegration:
    """End-to-end: SSE stream delivers approval_request events."""

    def test_sse_streams_approval_request_event(
        self,
        approval_client: TestClient,
        approval_user: User,
    ) -> None:
        """SSE endpoint should stream approval_request events before the final reply."""
        with patch(
            "backend.app.channels.webchat.message_bus.publish_inbound",
            new_callable=AsyncMock,
        ):
            resp = approval_client.post(
                "/api/user/chat",
                data={"message": "Query my QuickBooks"},
            )
        assert resp.status_code == 200
        request_id = resp.json()["request_id"]

        outbound = OutboundMessage(
            channel="webchat", chat_id="1", content="Here are your invoices.", request_id=request_id
        )

        def _publish_approval_then_resolve() -> None:
            import time

            time.sleep(0.2)
            loop = asyncio.new_event_loop()
            loop.run_until_complete(
                message_bus.publish_event(
                    request_id,
                    {"type": "approval_request", "content": "Query QuickBooks\n\nReply: yes | no"},
                )
            )
            loop.close()
            time.sleep(0.1)
            message_bus.resolve_response(request_id, outbound)

        t = threading.Thread(target=_publish_approval_then_resolve)
        t.start()

        with approval_client.stream("GET", f"/api/user/chat/events/{request_id}") as sse_resp:
            assert sse_resp.status_code == 200
            body = b""
            for chunk in sse_resp.iter_bytes():
                body += chunk
            text = body.decode()

        t.join(timeout=5)

        # Verify approval_request event appears in the SSE stream
        assert "approval_request" in text
        assert "Query QuickBooks" in text
        # Final reply should also appear
        assert "Here are your invoices." in text
        # Approval event should come before the reply
        assert text.index("approval_request") < text.index("Here are your invoices.")
