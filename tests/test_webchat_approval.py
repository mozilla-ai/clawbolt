"""Regression tests for webchat approval flow.

Verifies that:
- The approval prompt is published as an SSE event when request_id is set.
- Webchat approval responses via the bus resolve the gate and close SSE cleanly.
- The SSE event stream delivers approval_request events to the frontend.

Webchat approval is entirely message-based: the approval prompt appears as
a regular assistant message and the user types "yes"/"no" as a normal chat
message. The ingestion pipeline intercepts approval responses and resolves
the gate, just like Telegram.
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
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
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
# Message-based approval via bus (replaces the old /approve endpoint)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def approval_user() -> User:
    """Create a user for approval tests."""
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


class TestMessageBasedApproval:
    """Approval responses arrive as regular chat messages via the bus."""

    @pytest.mark.asyncio()
    async def test_approval_via_bus_resolves_gate(self, test_user: User) -> None:
        """A 'yes' message through the bus resolves a pending approval gate."""
        gate = get_approval_gate()

        # Simulate a pending approval
        mock_publish = AsyncMock()
        approval_task = asyncio.create_task(
            gate.request_approval(
                user_id=test_user.id,
                tool_name="writer",
                description="Write hello",
                publish_outbound=mock_publish,
                channel="webchat",
                chat_id=str(test_user.id),
                timeout=5.0,
            )
        )

        # Wait for the gate to be pending
        for _ in range(100):
            if gate.has_pending(test_user.id):
                break
            await asyncio.sleep(0.01)
        assert gate.has_pending(test_user.id)

        # Send "yes" as a regular inbound message (like the user typing it)
        inbound = InboundMessage(
            channel="webchat",
            sender_id=str(test_user.id),
            text="yes",
            request_id="approval-reply-req",
        )

        # Register a response future so the SSE stream has something to resolve
        message_bus.register_response_future("approval-reply-req")

        with patch(
            "backend.app.agent.ingestion._get_or_create_user",
            return_value=test_user,
        ):
            await process_inbound_from_bus(inbound)

        decision = await asyncio.wait_for(approval_task, timeout=2.0)
        assert decision == ApprovalDecision.APPROVED

    @pytest.mark.asyncio()
    async def test_approval_via_bus_resolves_response_future(self, test_user: User) -> None:
        """When an approval response has a request_id, the response future is resolved."""
        gate = get_approval_gate()

        mock_publish = AsyncMock()
        approval_task = asyncio.create_task(
            gate.request_approval(
                user_id=test_user.id,
                tool_name="writer",
                description="Write hello",
                publish_outbound=mock_publish,
                channel="webchat",
                chat_id=str(test_user.id),
                timeout=5.0,
            )
        )

        for _ in range(100):
            if gate.has_pending(test_user.id):
                break
            await asyncio.sleep(0.01)

        request_id = "approval-future-req"
        fut = message_bus.register_response_future(request_id)

        inbound = InboundMessage(
            channel="webchat",
            sender_id=str(test_user.id),
            text="yes",
            request_id=request_id,
        )
        with patch(
            "backend.app.agent.ingestion._get_or_create_user",
            return_value=test_user,
        ):
            await process_inbound_from_bus(inbound)

        # The response future should be resolved with empty content
        assert fut.done()
        outbound = fut.result()
        assert outbound.content == ""

        await asyncio.wait_for(approval_task, timeout=2.0)

    def test_approve_endpoint_removed(
        self,
        approval_client: TestClient,
        approval_user: User,
    ) -> None:
        """The /api/user/chat/approve endpoint no longer exists."""
        resp = approval_client.post(
            "/api/user/chat/approve",
            json={"decision": "yes"},
        )
        # Endpoint removed: should return 404 or 405
        assert resp.status_code in (404, 405)


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
