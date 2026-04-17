"""Tests for typing indicator integration with the agent loop, heartbeat, and ingestion."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.events import AgentEndEvent, ToolExecutionStartEvent, TurnStartEvent
from backend.app.agent.file_store import SessionState, StoredMessage
from backend.app.agent.heartbeat import evaluate_heartbeat_need
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
from backend.app.agent.router import _create_activity_forwarder
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.bus import MessageBus, OutboundMessage, message_bus
from backend.app.models import User
from tests.mocks.llm import make_text_response, make_tool_call_response


class _InputParams(BaseModel):
    """Params model for tools accepting an input parameter."""

    input: str


# ---------------------------------------------------------------------------
# ClawboltAgent typing indicator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_sends_typing_indicator_before_llm_call(
    mock_amessages: object, test_user: User
) -> None:
    """Agent should send a typing indicator before each acompletion call."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_publish = AsyncMock()

    agent = ClawboltAgent(
        user=test_user,
        channel="telegram",
        publish_outbound=mock_publish,
        chat_id="123456789",
    )
    await agent.process_message("Hi there")

    # Check that a typing indicator OutboundMessage was published
    mock_publish.assert_called()
    typing_calls = [
        c
        for c in mock_publish.call_args_list
        if isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    assert len(typing_calls) == 1
    assert typing_calls[0].args[0].chat_id == "123456789"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_sends_typing_indicator_before_each_tool_round(
    mock_amessages: object,
    test_user: User,
) -> None:
    """Agent should send typing indicator before each LLM call in multi-round tool loops."""

    async def mock_tool_fn(**kwargs: object) -> ToolResult:
        return ToolResult(content="tool result")

    tool = Tool(
        name="test_tool",
        description="A test tool",
        function=mock_tool_fn,
        params_model=_InputParams,
    )

    # First call returns a tool call, second call returns a text response
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(
            [{"name": "test_tool", "arguments": json.dumps({"input": "test"})}],
            content=None,
        ),
        make_text_response("Done!"),
    ]

    mock_publish = AsyncMock()

    agent = ClawboltAgent(
        user=test_user,
        channel="telegram",
        publish_outbound=mock_publish,
        chat_id="123456789",
    )
    agent.register_tools([tool])
    response = await agent.process_message("Do something")

    assert response.reply_text == "Done!"
    # Called three times: before initial LLM call, before tool execution, before second LLM call
    typing_calls = [
        c
        for c in mock_publish.call_args_list
        if isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    assert len(typing_calls) == 3
    assert typing_calls[0].args[0].chat_id == "123456789"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_works_without_publish_outbound(
    mock_amessages: object, test_user: User
) -> None:
    """Agent should work correctly when no publish_outbound is provided."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    agent = ClawboltAgent(user=test_user)
    response = await agent.process_message("Hi there")

    assert response.reply_text == "Hello!"
    mock_amessages.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_typing_indicator_failure_does_not_break_agent(
    mock_amessages: object, test_user: User
) -> None:
    """Agent should continue processing even if typing indicator fails."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_publish = AsyncMock(side_effect=RuntimeError("API down"))

    agent = ClawboltAgent(
        user=test_user,
        channel="telegram",
        publish_outbound=mock_publish,
        chat_id="123456789",
    )
    response = await agent.process_message("Hi there")

    assert response.reply_text == "Hello!"
    mock_publish.assert_called()


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_no_typing_indicator_without_chat_id(
    mock_amessages: object, test_user: User
) -> None:
    """Agent should not send typing indicator when chat_id is not provided."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_publish = AsyncMock()

    agent = ClawboltAgent(
        user=test_user,
        channel="telegram",
        publish_outbound=mock_publish,
        chat_id=None,
    )
    await agent.process_message("Hi there")

    # No typing indicator should be published (no chat_id)
    typing_calls = [
        c
        for c in mock_publish.call_args_list
        if c.args and isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    assert len(typing_calls) == 0


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_sends_typing_indicator_per_tool_in_round(
    mock_amessages: object,
    test_user: User,
) -> None:
    """Multiple tools in one round should each get a typing indicator."""

    async def mock_tool_fn(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    tool_a = Tool(
        name="tool_a",
        description="Tool A",
        function=mock_tool_fn,
        params_model=_InputParams,
    )
    tool_b = Tool(
        name="tool_b",
        description="Tool B",
        function=mock_tool_fn,
        params_model=_InputParams,
    )

    # LLM requests two tools in parallel, then returns text
    mock_amessages.side_effect = [  # type: ignore[union-attr]
        make_tool_call_response(
            [
                {"name": "tool_a", "arguments": json.dumps({"input": "a"})},
                {"name": "tool_b", "arguments": json.dumps({"input": "b"})},
            ],
            content=None,
        ),
        make_text_response("All done!"),
    ]

    mock_publish = AsyncMock()

    agent = ClawboltAgent(
        user=test_user,
        channel="telegram",
        publish_outbound=mock_publish,
        chat_id="123456789",
    )
    agent.register_tools([tool_a, tool_b])
    response = await agent.process_message("Do two things")

    assert response.reply_text == "All done!"
    typing_calls = [
        c
        for c in mock_publish.call_args_list
        if isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    # 1 before initial LLM + 2 before each tool execution + 1 before second LLM = 4
    assert len(typing_calls) == 4


# ---------------------------------------------------------------------------
# Activity stream tests (user-level, cross-channel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_bus_activity_queue_register_publish_remove() -> None:
    """Activity queues should receive events and clean up on removal."""
    bus = MessageBus()
    q1 = bus.register_activity_queue("user-1")
    q2 = bus.register_activity_queue("user-1")

    await bus.publish_activity("user-1", {"type": "thinking"})

    assert not q1.empty()
    assert not q2.empty()
    assert (await q1.get()) == {"type": "thinking"}
    assert (await q2.get()) == {"type": "thinking"}

    bus.remove_activity_queue("user-1", q1)
    await bus.publish_activity("user-1", {"type": "done"})

    # Only q2 should receive (q1 was removed)
    assert q1.empty()
    assert not q2.empty()
    assert (await q2.get()) == {"type": "done"}

    bus.remove_activity_queue("user-1", q2)
    assert "user-1" not in bus._activity_queues


@pytest.mark.asyncio()
async def test_bus_activity_replays_last_event_on_register() -> None:
    """New activity subscribers should immediately receive the last published event.

    This covers the case where an SSE client reconnects after missing the
    'done' event: the reconnected queue should get 'done' right away so
    the frontend clears the spinner.
    """
    bus = MessageBus()
    q1 = bus.register_activity_queue("user-1")

    await bus.publish_activity("user-1", {"type": "thinking"})
    await bus.publish_activity("user-1", {"type": "done"})

    # Drain q1
    assert (await q1.get()) == {"type": "thinking"}
    assert (await q1.get()) == {"type": "done"}

    # Simulate disconnect + reconnect: remove old queue, register new one
    bus.remove_activity_queue("user-1", q1)
    q2 = bus.register_activity_queue("user-1")

    # q2 should get the last event ("done") replayed immediately
    assert not q2.empty()
    assert (await q2.get()) == {"type": "done"}


@pytest.mark.asyncio()
async def test_bus_activity_no_replay_without_prior_events() -> None:
    """When no activity has been published, new queues should start empty."""
    bus = MessageBus()
    q = bus.register_activity_queue("user-1")
    assert q.empty()


@pytest.mark.asyncio()
async def test_bus_publish_activity_no_subscribers() -> None:
    """Publishing activity with no subscribers should not raise."""
    bus = MessageBus()
    await bus.publish_activity("no-one", {"type": "thinking"})


@pytest.mark.asyncio()
async def test_activity_forwarder_turn_start() -> None:
    """Activity forwarder should emit 'thinking' on TurnStartEvent."""
    with patch("backend.app.bus.message_bus") as mock_bus:
        mock_bus.publish_activity = AsyncMock()

        forwarder = _create_activity_forwarder("user-1", "telegram")
        await forwarder(TurnStartEvent(round_number=1, message_count=5))

        mock_bus.publish_activity.assert_called_once_with(
            "user-1", {"type": "thinking", "channel": "telegram"}
        )


@pytest.mark.asyncio()
async def test_activity_forwarder_tool_execution() -> None:
    """Activity forwarder should emit 'tool_call' on ToolExecutionStartEvent."""
    with patch("backend.app.bus.message_bus") as mock_bus:
        mock_bus.publish_activity = AsyncMock()

        forwarder = _create_activity_forwarder("user-1", "telegram")
        await forwarder(ToolExecutionStartEvent(tool_name="search_web", arguments={}))

        mock_bus.publish_activity.assert_called_once_with(
            "user-1",
            {"type": "tool_call", "tool_name": "search_web", "channel": "telegram"},
        )


@pytest.mark.asyncio()
async def test_activity_forwarder_agent_end() -> None:
    """Activity forwarder should emit 'done' on AgentEndEvent."""
    with patch("backend.app.bus.message_bus") as mock_bus:
        mock_bus.publish_activity = AsyncMock()

        forwarder = _create_activity_forwarder("user-1", "telegram")
        await forwarder(AgentEndEvent(reply_text="Done"))

        mock_bus.publish_activity.assert_called_once_with(
            "user-1", {"type": "done", "channel": "telegram"}
        )


# ---------------------------------------------------------------------------
# Heartbeat typing indicator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.log_llm_usage")
@patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
@patch("backend.app.agent.heartbeat.HeartbeatStore")
@patch("backend.app.agent.heartbeat.get_session_store")
@patch("backend.app.agent.heartbeat.settings")
@patch("backend.app.agent.heartbeat.amessages")
@patch("backend.app.bus.message_bus")
async def test_heartbeat_sends_typing_indicator_before_llm_call(
    mock_bus: MagicMock,
    mock_llm: AsyncMock,
    mock_settings: MagicMock,
    mock_get_session_store: MagicMock,
    mock_heartbeat_store_cls: MagicMock,
    mock_build_prompt: AsyncMock,
    mock_log_usage: MagicMock,
    test_user: User,
) -> None:
    """Heartbeat should send typing indicator before calling the LLM."""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_settings.llm_api_base = None
    mock_settings.heartbeat_model = ""
    mock_settings.heartbeat_provider = ""
    mock_settings.llm_max_tokens_heartbeat = 256
    mock_settings.heartbeat_recent_messages_count = 5

    mock_session_store = MagicMock()
    mock_session_store.get_recent_messages.return_value = []
    mock_get_session_store.return_value = mock_session_store

    mock_hb_store = MagicMock()
    mock_hb_store.read_heartbeat_md.return_value = ""
    mock_hb_store.get_recent_logs = AsyncMock(return_value=[])
    mock_heartbeat_store_cls.return_value = mock_hb_store

    mock_build_prompt.return_value = "system prompt"

    mock_llm.return_value = make_tool_call_response(
        [
            {
                "name": "compose_message",
                "arguments": json.dumps(
                    {
                        "action": "no_action",
                        "message": "",
                        "reasoning": "Nothing actionable",
                        "priority": 1,
                    }
                ),
            }
        ],
    )

    mock_bus.publish_outbound = AsyncMock()

    await evaluate_heartbeat_need(
        test_user,
        channel="telegram",
        chat_id=test_user.channel_identifier,
    )

    # Check that a typing indicator was published to the bus
    mock_bus.publish_outbound.assert_called()
    typing_calls = [
        c
        for c in mock_bus.publish_outbound.call_args_list
        if isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    assert len(typing_calls) == 1
    assert typing_calls[0].args[0].chat_id == test_user.channel_identifier


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.log_llm_usage")
@patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
@patch("backend.app.agent.heartbeat.HeartbeatStore")
@patch("backend.app.agent.heartbeat.get_session_store")
@patch("backend.app.agent.heartbeat.settings")
@patch("backend.app.agent.heartbeat.amessages")
async def test_heartbeat_works_without_channel(
    mock_llm: AsyncMock,
    mock_settings: MagicMock,
    mock_get_session_store: MagicMock,
    mock_heartbeat_store_cls: MagicMock,
    mock_build_prompt: AsyncMock,
    mock_log_usage: MagicMock,
    test_user: User,
) -> None:
    """Heartbeat should work when no channel is provided."""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_settings.llm_api_base = None
    mock_settings.heartbeat_model = ""
    mock_settings.heartbeat_provider = ""
    mock_settings.llm_max_tokens_heartbeat = 256
    mock_settings.heartbeat_recent_messages_count = 5

    mock_session_store = MagicMock()
    mock_session_store.get_recent_messages.return_value = []
    mock_get_session_store.return_value = mock_session_store

    mock_hb_store = MagicMock()
    mock_hb_store.read_heartbeat_md.return_value = ""
    mock_hb_store.get_recent_logs = AsyncMock(return_value=[])
    mock_heartbeat_store_cls.return_value = mock_hb_store

    mock_build_prompt.return_value = "system prompt"

    mock_llm.return_value = make_tool_call_response(
        [
            {
                "name": "heartbeat_decision",
                "arguments": json.dumps(
                    {
                        "action": "skip",
                        "tasks": "",
                        "reasoning": "Nothing actionable",
                    }
                ),
            }
        ],
    )

    # Should not raise when no channel is provided
    decision = await evaluate_heartbeat_need(test_user)
    assert decision.action == "skip"


# ---------------------------------------------------------------------------
# Early typing indicator (ingestion) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_early_typing_indicator_published_on_inbound() -> None:
    """process_inbound_from_bus should publish a typing indicator before dispatch."""
    inbound = InboundMessage(
        channel="bluebubbles",
        sender_id="+15551234567",
        text="hello",
    )

    mock_user = User(id="1", channel_identifier="+15551234567", phone="")
    mock_session = SessionState(session_id="sess-1", user_id="1")
    mock_message = StoredMessage(direction="inbound", body="hello")

    with (
        patch(
            "backend.app.agent.ingestion._get_or_create_user",
            new_callable=AsyncMock,
            return_value=mock_user,
        ),
        patch(
            "backend.app.agent.ingestion.get_approval_gate",
        ) as mock_gate,
        patch(
            "backend.app.agent.ingestion.get_or_create_conversation",
            new_callable=AsyncMock,
            return_value=(mock_session, True),
        ),
        patch(
            "backend.app.agent.ingestion.get_session_store",
        ) as mock_store_fn,
        patch(
            "backend.app.agent.ingestion.settings",
        ) as mock_settings,
        patch(
            "backend.app.agent.ingestion._dispatch_to_pipeline",
            new_callable=AsyncMock,
        ) as mock_dispatch,
    ):
        mock_gate.return_value.has_pending.return_value = False
        mock_session_store = AsyncMock()
        mock_session_store.add_message.return_value = mock_message
        mock_store_fn.return_value = mock_session_store
        mock_settings.message_batch_window_ms = 0

        await process_inbound_from_bus(inbound)

        # A typing indicator should have been published to the bus
        typing_found = False
        while not message_bus.outbound.empty():
            outbound = message_bus.outbound.get_nowait()
            if outbound.is_typing_indicator:
                assert outbound.channel == "bluebubbles"
                assert outbound.chat_id == "+15551234567"
                typing_found = True
                break
        assert typing_found, "Expected an early typing indicator on the outbound bus"

        # Pipeline dispatch should still have been called
        mock_dispatch.assert_called_once()


@pytest.mark.asyncio()
async def test_early_typing_indicator_swallows_bus_errors() -> None:
    """_send_early_typing_indicator should not raise even when the bus fails."""
    from backend.app.agent.ingestion import _send_early_typing_indicator

    with patch("backend.app.bus.message_bus") as mock_bus:
        mock_bus.publish_outbound = AsyncMock(side_effect=RuntimeError("bus exploded"))

        # Should not raise
        await _send_early_typing_indicator("bluebubbles", "+15551234567")
