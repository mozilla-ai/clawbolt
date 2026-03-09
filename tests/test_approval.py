"""Tests for the progressive approval system."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalPolicy,
    ApprovalStore,
    PermissionLevel,
    _format_approval_message,
    _parse_approval_response,
    get_approval_gate,
    get_approval_store,
    reset_approval_gate,
)
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import UserData
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.services.messaging import MessagingService
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    text: str


async def _echo_tool(text: str) -> ToolResult:
    return ToolResult(content=f"echo: {text}")


class _UrlParams(BaseModel):
    url: str


async def _fetch_tool(url: str) -> ToolResult:
    return ToolResult(content=f"fetched: {url}")


def _extract_domain(args: dict[str, object]) -> str | None:
    from urllib.parse import urlparse

    url = str(args.get("url", ""))
    parsed = urlparse(url)
    return parsed.netloc or None


def _describe_fetch(args: dict[str, object]) -> str:
    return f"fetch content from {args.get('url', 'unknown URL')}"


# ---------------------------------------------------------------------------
# ApprovalStore
# ---------------------------------------------------------------------------


class TestApprovalStore:
    def test_default_permission(self, tmp_path: object) -> None:
        store = ApprovalStore()
        level = store.check_permission(1, "web_search", default=PermissionLevel.ASK)
        assert level == PermissionLevel.ASK

    def test_tool_level_override(self, tmp_path: object) -> None:
        store = ApprovalStore()
        store.set_permission(1, "web_search", PermissionLevel.AUTO)
        level = store.check_permission(1, "web_search", default=PermissionLevel.ASK)
        assert level == PermissionLevel.AUTO

    def test_resource_level_override(self, tmp_path: object) -> None:
        store = ApprovalStore()
        store.set_permission(1, "web_fetch", PermissionLevel.AUTO, resource="homedepot.com")
        level = store.check_permission(
            1, "web_fetch", resource="homedepot.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.AUTO

    def test_glob_matching(self, tmp_path: object) -> None:
        store = ApprovalStore()
        store.set_permission(1, "web_fetch", PermissionLevel.AUTO, resource="*.gov")
        level = store.check_permission(
            1, "web_fetch", resource="permits.gov", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.AUTO

    def test_resource_priority_over_tool(self, tmp_path: object) -> None:
        store = ApprovalStore()
        store.set_permission(1, "web_fetch", PermissionLevel.DENY)
        store.set_permission(1, "web_fetch", PermissionLevel.AUTO, resource="safe.com")
        level = store.check_permission(
            1, "web_fetch", resource="safe.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.AUTO

    def test_falls_through_to_tool_when_no_resource_match(self, tmp_path: object) -> None:
        store = ApprovalStore()
        store.set_permission(1, "web_fetch", PermissionLevel.DENY)
        level = store.check_permission(
            1, "web_fetch", resource="unknown.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.DENY

    def test_persistence_round_trip(self, tmp_path: object) -> None:
        store1 = ApprovalStore()
        store1.set_permission(1, "web_search", PermissionLevel.AUTO)
        store1.set_permission(1, "web_fetch", PermissionLevel.DENY, resource="evil.com")

        store2 = ApprovalStore()
        assert store2.check_permission(1, "web_search") == PermissionLevel.AUTO
        assert store2.check_permission(1, "web_fetch", resource="evil.com") == PermissionLevel.DENY


# ---------------------------------------------------------------------------
# _parse_approval_response
# ---------------------------------------------------------------------------


class TestParseApprovalResponse:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("yes", ApprovalDecision.APPROVED),
            ("y", ApprovalDecision.APPROVED),
            ("Yes", ApprovalDecision.APPROVED),
            ("YES", ApprovalDecision.APPROVED),
            ("  y  ", ApprovalDecision.APPROVED),
            ("always", ApprovalDecision.ALWAYS_ALLOW),
            ("Always", ApprovalDecision.ALWAYS_ALLOW),
            ("no", ApprovalDecision.DENIED),
            ("n", ApprovalDecision.DENIED),
            ("No", ApprovalDecision.DENIED),
            ("never", ApprovalDecision.ALWAYS_DENY),
            ("Never", ApprovalDecision.ALWAYS_DENY),
        ],
    )
    def test_valid_responses(self, text: str, expected: ApprovalDecision) -> None:
        assert _parse_approval_response(text) == expected

    @pytest.mark.parametrize("text", ["maybe", "sure", "ok", "hello", ""])
    def test_unrecognized_returns_none(self, text: str) -> None:
        assert _parse_approval_response(text) is None


# ---------------------------------------------------------------------------
# _format_approval_message
# ---------------------------------------------------------------------------


class TestFormatApprovalMessage:
    def test_output_format(self) -> None:
        msg = _format_approval_message("web_fetch", "fetch content from https://example.com")
        assert "web_fetch" in msg
        assert "fetch content from https://example.com" in msg
        assert "yes" in msg
        assert "no" in msg
        assert "always" in msg
        assert "never" in msg


# ---------------------------------------------------------------------------
# ApprovalGate
# ---------------------------------------------------------------------------


class TestApprovalGate:
    @pytest.mark.asyncio()
    async def test_resolve_sets_event_and_decision(self) -> None:
        gate = ApprovalGate()
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")

        async def _resolve_soon() -> None:
            await asyncio.sleep(0.01)
            gate.resolve(1, ApprovalDecision.APPROVED)

        task = asyncio.create_task(_resolve_soon())
        decision = await gate.request_approval(
            user_id=1,
            tool_name="test_tool",
            description="test description",
            messaging_service=mock_service,
            chat_id="chat_1",
            timeout=5.0,
        )
        await task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending(1)

    @pytest.mark.asyncio()
    async def test_timeout_returns_denied(self) -> None:
        gate = ApprovalGate()
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")

        decision = await gate.request_approval(
            user_id=1,
            tool_name="test_tool",
            description="test description",
            messaging_service=mock_service,
            chat_id="chat_1",
            timeout=0.01,
        )
        assert decision == ApprovalDecision.DENIED
        assert not gate.has_pending(1)

    def test_resolve_returns_false_when_nothing_pending(self) -> None:
        gate = ApprovalGate()
        assert gate.resolve(999, ApprovalDecision.APPROVED) is False

    @pytest.mark.asyncio()
    async def test_has_pending(self) -> None:
        gate = ApprovalGate()
        assert not gate.has_pending(1)

        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")

        async def _check_and_resolve() -> None:
            await asyncio.sleep(0.01)
            assert gate.has_pending(1)
            gate.resolve(1, ApprovalDecision.DENIED)

        task = asyncio.create_task(_check_and_resolve())
        await gate.request_approval(
            user_id=1,
            tool_name="t",
            description="d",
            messaging_service=mock_service,
            chat_id="c",
            timeout=5.0,
        )
        await task


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


class TestAgentApproval:
    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_without_policy_executes_normally(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """Tools without approval_policy execute unchanged."""
        tool = Tool(
            name="echo",
            description="Echo text",
            function=_echo_tool,
            params_model=_EchoParams,
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "echo", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("echo hello")
        assert response.reply_text == "Done!"
        assert any(tc.name == "echo" and not tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_auto_skips_gate(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """Tool with AUTO default_level executes without prompting."""
        tool = Tool(
            name="echo",
            description="Echo text",
            function=_echo_tool,
            params_model=_EchoParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.AUTO),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "echo", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("echo hello")
        assert any(tc.name == "echo" and not tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_deny_returns_error(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """Tool with DENY default_level returns a permission error."""
        tool = Tool(
            name="dangerous",
            description="Dangerous tool",
            function=_echo_tool,
            params_model=_EchoParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.DENY),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "dangerous", "arguments": {"text": "boom"}}]),
            make_text_response("Denied!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("do it")
        assert any(tc.name == "dangerous" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_ask_approved_executes(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """Tool with ASK that gets approved executes."""
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=_describe_fetch,
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Fetched!"),
        ]

        gate = get_approval_gate()

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(user=test_user, messaging_service=mock_service, chat_id="chat_1")
        agent.register_tools([tool])

        task = asyncio.create_task(_approve_soon())
        response = await agent.process_message("fetch example.com")
        await task

        assert any(tc.name == "fetcher" and not tc.is_error for tc in response.tool_calls)
        mock_service.send_text.assert_called()

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_ask_denied_returns_error(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """Tool with ASK that gets denied returns an error."""
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Denied!"),
        ]

        gate = get_approval_gate()

        async def _deny_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.DENIED)

        agent = ClawboltAgent(user=test_user, messaging_service=mock_service, chat_id="chat_1")
        agent.register_tools([tool])

        task = asyncio.create_task(_deny_soon())
        response = await agent.process_message("fetch example.com")
        await task

        assert any(tc.name == "fetcher" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_persists_auto_to_store(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """'always' decision persists AUTO to the approval store."""
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW)

        agent = ClawboltAgent(user=test_user, messaging_service=mock_service, chat_id="chat_1")
        agent.register_tools([tool])

        task = asyncio.create_task(_always_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        level = store.check_permission(test_user.id, "fetcher")
        assert level == PermissionLevel.AUTO

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_never_persists_deny_to_store(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """'never' decision persists DENY to the approval store."""
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Blocked!"),
        ]

        gate = get_approval_gate()

        async def _never_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            gate.resolve(test_user.id, ApprovalDecision.ALWAYS_DENY)

        agent = ClawboltAgent(user=test_user, messaging_service=mock_service, chat_id="chat_1")
        agent.register_tools([tool])

        task = asyncio.create_task(_never_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        level = store.check_permission(test_user.id, "fetcher")
        assert level == PermissionLevel.DENY

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_stored_auto_skips_prompt(
        self, mock_amessages: object, test_user: UserData
    ) -> None:
        """A stored AUTO permission skips the approval prompt entirely."""
        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        store = get_approval_store()
        store.set_permission(test_user.id, "fetcher", PermissionLevel.AUTO)

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        agent = ClawboltAgent(user=test_user, messaging_service=mock_service, chat_id="chat_1")
        agent.register_tools([tool])

        response = await agent.process_message("fetch example.com")
        assert any(tc.name == "fetcher" and not tc.is_error for tc in response.tool_calls)
        # send_text should only be called for typing indicator, not approval prompt
        for call in mock_service.send_text.call_args_list:
            assert "wants to use" not in str(call)


# ---------------------------------------------------------------------------
# Ingestion intercept
# ---------------------------------------------------------------------------


class TestIngestionIntercept:
    @pytest.mark.asyncio()
    async def test_approval_response_resolves_gate(self, test_user: UserData) -> None:
        """An approval response resolves the gate and skips normal processing."""
        gate = get_approval_gate()

        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")

        # Start a pending approval
        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                messaging_service=mock_service,
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await asyncio.sleep(0.01)
        assert gate.has_pending(test_user.id)

        # Simulate inbound "yes" message
        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="yes",
        )

        with patch(
            "backend.app.agent.ingestion._get_or_create_user",
            new_callable=AsyncMock,
            return_value=test_user,
        ):
            await process_inbound_from_bus(inbound, mock_service)

        decision = await approval_task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending(test_user.id)

    @pytest.mark.asyncio()
    async def test_non_approval_text_during_pending_falls_through(
        self, test_user: UserData
    ) -> None:
        """Unrecognized text while pending falls through to normal processing."""
        gate = get_approval_gate()

        mock_service = MagicMock(spec=MessagingService)
        mock_service.send_text = AsyncMock(return_value="msg_id")
        mock_service.send_typing_indicator = AsyncMock()

        # Start a pending approval
        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                messaging_service=mock_service,
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await asyncio.sleep(0.01)
        assert gate.has_pending(test_user.id)

        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="what is the weather?",
        )

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ),
            patch(
                "backend.app.agent.ingestion.settings.message_batch_window_ms",
                0,
            ),
        ):
            await process_inbound_from_bus(inbound, mock_service)

        # The gate should still be pending (text was not a valid response)
        assert gate.has_pending(test_user.id)

        # Resolve the gate so the task completes
        gate.resolve(test_user.id, ApprovalDecision.DENIED)
        await approval_task


# ---------------------------------------------------------------------------
# Module-level accessors
# ---------------------------------------------------------------------------


class TestModuleAccessors:
    def test_get_approval_gate_returns_singleton(self) -> None:
        g1 = get_approval_gate()
        g2 = get_approval_gate()
        assert g1 is g2

    def test_get_approval_store_returns_singleton(self) -> None:
        s1 = get_approval_store()
        s2 = get_approval_store()
        assert s1 is s2

    def test_reset_clears_singletons(self) -> None:
        g1 = get_approval_gate()
        s1 = get_approval_store()
        reset_approval_gate()
        g2 = get_approval_gate()
        s2 = get_approval_store()
        assert g1 is not g2
        assert s1 is not s2
