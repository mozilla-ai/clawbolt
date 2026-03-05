"""Tests for typing indicator integration with the agent loop and heartbeat."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.agent.core import ClawboltAgent
from backend.app.agent.heartbeat import evaluate_heartbeat_need
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.models import Contractor
from backend.app.services.messaging import MessagingService
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
    mock_amessages: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should send a typing indicator before each acompletion call."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_messaging = MagicMock(spec=MessagingService)
    mock_messaging.send_typing_indicator = AsyncMock()

    agent = ClawboltAgent(
        db=db_session,
        contractor=test_contractor,
        messaging_service=mock_messaging,
        chat_id="123456789",
    )
    await agent.process_message("Hi there")

    mock_messaging.send_typing_indicator.assert_called_once_with(to="123456789")


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_sends_typing_indicator_before_each_tool_round(
    mock_amessages: object,
    db_session: Session,
    test_contractor: Contractor,
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

    mock_messaging = MagicMock(spec=MessagingService)
    mock_messaging.send_typing_indicator = AsyncMock()

    agent = ClawboltAgent(
        db=db_session,
        contractor=test_contractor,
        messaging_service=mock_messaging,
        chat_id="123456789",
    )
    agent.register_tools([tool])
    response = await agent.process_message("Do something")

    assert response.reply_text == "Done!"
    # Called twice: once before initial LLM call, once before second LLM call after tool execution
    assert mock_messaging.send_typing_indicator.call_count == 2
    mock_messaging.send_typing_indicator.assert_called_with(to="123456789")


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_works_without_messaging_service(
    mock_amessages: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should work correctly when no messaging_service is provided."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    agent = ClawboltAgent(db=db_session, contractor=test_contractor)
    response = await agent.process_message("Hi there")

    assert response.reply_text == "Hello!"
    mock_amessages.assert_called_once()  # type: ignore[union-attr]


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_typing_indicator_failure_does_not_break_agent(
    mock_amessages: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should continue processing even if typing indicator fails."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_messaging = MagicMock(spec=MessagingService)
    mock_messaging.send_typing_indicator = AsyncMock(side_effect=RuntimeError("API down"))

    agent = ClawboltAgent(
        db=db_session,
        contractor=test_contractor,
        messaging_service=mock_messaging,
        chat_id="123456789",
    )
    response = await agent.process_message("Hi there")

    assert response.reply_text == "Hello!"
    mock_messaging.send_typing_indicator.assert_called_once_with(to="123456789")


@pytest.mark.asyncio()
@patch("backend.app.agent.core.amessages")
async def test_agent_no_typing_indicator_without_chat_id(
    mock_amessages: object, db_session: Session, test_contractor: Contractor
) -> None:
    """Agent should not send typing indicator when chat_id is not provided."""
    mock_amessages.return_value = make_text_response("Hello!")  # type: ignore[union-attr]

    mock_messaging = MagicMock(spec=MessagingService)
    mock_messaging.send_typing_indicator = AsyncMock()

    agent = ClawboltAgent(
        db=db_session,
        contractor=test_contractor,
        messaging_service=mock_messaging,
        chat_id=None,
    )
    await agent.process_message("Hi there")

    mock_messaging.send_typing_indicator.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat typing indicator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.settings")
@patch("backend.app.agent.heartbeat.amessages")
async def test_heartbeat_sends_typing_indicator_before_llm_call(
    mock_llm: AsyncMock,
    mock_settings: MagicMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Heartbeat should send typing indicator before calling the LLM."""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_settings.llm_api_base = None
    mock_settings.heartbeat_model = ""
    mock_settings.heartbeat_provider = ""
    mock_settings.llm_max_tokens_heartbeat = 256

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

    mock_messaging = MagicMock(spec=MessagingService)
    mock_messaging.send_typing_indicator = AsyncMock()

    await evaluate_heartbeat_need(
        db_session,
        test_contractor,
        ["Stale draft estimate"],
        messaging_service=mock_messaging,
    )

    mock_messaging.send_typing_indicator.assert_called_once_with(
        to=test_contractor.channel_identifier
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.heartbeat.settings")
@patch("backend.app.agent.heartbeat.amessages")
async def test_heartbeat_works_without_messaging_service(
    mock_llm: AsyncMock,
    mock_settings: MagicMock,
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Heartbeat should work when no messaging_service is provided."""
    mock_settings.llm_model = "test-model"
    mock_settings.llm_provider = "test-provider"
    mock_settings.llm_api_base = None
    mock_settings.heartbeat_model = ""
    mock_settings.heartbeat_provider = ""
    mock_settings.llm_max_tokens_heartbeat = 256

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

    # Should not raise when no messaging_service is provided
    action = await evaluate_heartbeat_need(
        db_session,
        test_contractor,
        ["Stale draft estimate"],
    )
    assert action.action_type == "no_action"
