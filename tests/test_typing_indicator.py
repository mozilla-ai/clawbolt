"""Tests for typing indicator integration with the agent loop and heartbeat."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.file_store import UserData
from backend.app.agent.heartbeat import evaluate_heartbeat_need
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.bus import OutboundMessage
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
    mock_amessages: object, test_user: UserData
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
    test_user: UserData,
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
    # Called twice: once before initial LLM call, once before second LLM call after tool execution
    typing_calls = [
        c
        for c in mock_publish.call_args_list
        if isinstance(c.args[0], OutboundMessage) and c.args[0].is_typing_indicator
    ]
    assert len(typing_calls) == 2
    assert typing_calls[0].args[0].chat_id == "123456789"


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_works_without_publish_outbound(
    mock_amessages: object, test_user: UserData
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
    mock_amessages: object, test_user: UserData
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
    mock_amessages: object, test_user: UserData
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
    test_user: UserData,
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
    mock_hb_store.read_checklist_md.return_value = ""
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
    test_user: UserData,
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
    mock_hb_store.read_checklist_md.return_value = ""
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
