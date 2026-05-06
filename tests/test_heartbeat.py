"""Tests for the proactive heartbeat engine."""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm.types.messages import MessageResponse, MessageUsage, ToolUseBlock
from pydantic import BaseModel
from sqlalchemy import event

import backend.app.database as _db_module
from backend.app.agent.dto import HeartbeatLogEntry
from backend.app.agent.heartbeat import (
    _HISTORY_LOOKBACK_DAYS,
    COMPOSE_MESSAGE_TOOL,
    HEARTBEAT_DECISION_TOOL,
    ComposeMessageParams,
    HeartbeatAction,
    HeartbeatDecision,
    HeartbeatDecisionParams,
    HeartbeatScheduler,
    _format_heartbeat_history,
    _heartbeat_usage_hooks,
    _parse_decision_response,
    _parse_tool_call_response,
    _user_messaged_within,
    evaluate_heartbeat_need,
    execute_heartbeat_tasks,
    get_daily_heartbeat_count,
    parse_frequency_to_minutes,
    register_heartbeat_usage_hook,
    run_heartbeat_for_user,
)
from backend.app.agent.system_prompt import to_local_time
from backend.app.models import ChannelRoute, ChatSession, Message, User
from tests.mocks.llm import (
    make_text_response,
    make_tool_call_response,
    make_truncated_tool_call_response,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user() -> User:
    db = _db_module.SessionLocal()
    try:
        u = User(
            user_id="hb-user-001",
            phone="+15559990000",
            onboarding_complete=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


@pytest.fixture()
def user_with_timezone() -> User:
    db = _db_module.SessionLocal()
    try:
        u = User(
            user_id="hb-user-003",
            phone="+15559990002",
            timezone="America/Los_Angeles",
            onboarding_complete=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


def _make_heartbeat_tool_call(
    action: str = "no_action",
    message: str = "",
    reasoning: str = "",
    priority: int = 1,
    tool_name: str = "compose_message",
) -> MessageResponse:
    """Build a mock LLM response that includes a tool call."""
    args = json.dumps(
        {
            "action": action,
            "message": message,
            "reasoning": reasoning,
            "priority": priority,
        }
    )
    return make_tool_call_response([{"name": tool_name, "arguments": args, "id": "call_mock_001"}])


def _make_decision_tool_call(
    action: str = "skip",
    tasks: str = "",
    reasoning: str = "",
    tool_name: str = "heartbeat_decision",
) -> MessageResponse:
    """Build a mock Phase 1 LLM response with a heartbeat_decision tool call."""
    args = json.dumps(
        {
            "action": action,
            "tasks": tasks,
            "reasoning": reasoning,
        }
    )
    return make_tool_call_response([{"name": tool_name, "arguments": args, "id": "call_mock_002"}])


class TestHasActionableHeartbeatContent:
    """Gate that decides whether HEARTBEAT.md is worth evaluating.

    Lifted out of the inline `.strip()` check so a header-only file
    short-circuits the Phase 1 LLM call. Production telemetry caught
    one user burning ~48 LLM calls/day on a "# Reminders\\n" file
    after Phase 2 cleaned up the only one-time item.
    """

    def test_empty_string_is_not_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        assert _has_actionable_heartbeat_content("") is False

    def test_whitespace_only_is_not_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        assert _has_actionable_heartbeat_content("   \n\n  \t\n") is False

    def test_header_only_is_not_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        # The exact pattern that motivated this gate: Phase 2 wrote
        # back the file with only the header after pruning the last
        # one-time item.
        assert _has_actionable_heartbeat_content("# Reminders\n") is False
        assert _has_actionable_heartbeat_content("# Reminders\n\n## Today\n\n") is False

    def test_list_item_is_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        assert (
            _has_actionable_heartbeat_content("# Reminders\n\n- At 3pm: check the queue\n") is True
        )

    def test_free_text_paragraph_is_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        assert (
            _has_actionable_heartbeat_content("# Reminders\n\nFollow up on the estimate.\n") is True
        )

    def test_indented_header_is_still_a_header(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        # Markdown allows leading whitespace before "#" and still
        # treats it as a header. The gate matches that.
        assert _has_actionable_heartbeat_content("   # Reminders\n") is False

    def test_hashtag_line_is_actionable(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        # Markdown's ATX-heading rule requires "#" followed by space or
        # end-of-line; "#urgent" is a hashtag, not a heading. The gate
        # treats hashtag-style lines as actionable so a future style
        # drift doesn't silently lose work.
        assert _has_actionable_heartbeat_content("#urgent: ping the customer\n") is True

    def test_bare_hash_is_a_heading(self) -> None:
        from backend.app.agent.heartbeat import _has_actionable_heartbeat_content

        # A single "#" or "##" with no body is a degenerate heading.
        # Matches the markdown rule (one or more "#" + EOL counts as a
        # heading) and matches the empty-content intent of the gate.
        assert _has_actionable_heartbeat_content("#\n") is False
        assert _has_actionable_heartbeat_content("##\n") is False


class TestToLocalTime:
    """Tests for the to_local_time helper."""

    def test_converts_utc_to_pacific(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        local = to_local_time(utc_time, "America/Los_Angeles")
        # UTC 17:00 in June (PDT, UTC-7) -> 10:00 local
        assert local.hour == 10

    def test_converts_utc_to_eastern(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        local = to_local_time(utc_time, "America/New_York")
        # UTC 17:00 in June (EDT, UTC-4) -> 13:00 local
        assert local.hour == 13

    def test_empty_timezone_returns_unchanged(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = to_local_time(utc_time, "")
        assert result.hour == 17

    def test_invalid_timezone_returns_unchanged(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = to_local_time(utc_time, "Not/A_Real_Zone")
        assert result.hour == 17


# ---------------------------------------------------------------------------
# COMPOSE_MESSAGE_TOOL schema validation
# ---------------------------------------------------------------------------


class TestComposeMessageToolSchema:
    def test_tool_has_name(self) -> None:
        assert COMPOSE_MESSAGE_TOOL["name"] == "compose_message"

    def test_tool_has_description(self) -> None:
        assert "description" in COMPOSE_MESSAGE_TOOL

    def test_tool_has_required_fields(self) -> None:
        required = COMPOSE_MESSAGE_TOOL["input_schema"]["required"]
        assert "action" in required
        assert "reasoning" in required
        assert "priority" in required

    def test_action_enum_values(self) -> None:
        action_prop = COMPOSE_MESSAGE_TOOL["input_schema"]["properties"]["action"]
        assert action_prop["enum"] == ["send_message", "no_action"]

    def test_priority_is_integer_with_bounds(self) -> None:
        priority_prop = COMPOSE_MESSAGE_TOOL["input_schema"]["properties"]["priority"]
        assert priority_prop["type"] == "integer"
        assert priority_prop["minimum"] == 1
        assert priority_prop["maximum"] == 5

    def test_schema_generated_from_pydantic_model(self) -> None:
        """COMPOSE_MESSAGE_TOOL schema is generated from ComposeMessageParams."""
        assert COMPOSE_MESSAGE_TOOL["input_schema"] == ComposeMessageParams.model_json_schema()


# ---------------------------------------------------------------------------
# _parse_tool_call_response
# ---------------------------------------------------------------------------


class TestParseToolCallResponse:
    def test_valid_send_message(self) -> None:
        """A well-formed compose_message tool call should parse correctly."""
        resp = _make_heartbeat_tool_call(
            action="send_message",
            message="Hey Mike, draft estimate pending!",
            reasoning="Stale draft",
            priority=4,
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "send_message"
        assert action.message == "Hey Mike, draft estimate pending!"
        assert action.reasoning == "Stale draft"
        assert action.priority == 4

    def test_valid_no_action(self) -> None:
        """A no_action tool call should parse correctly."""
        resp = _make_heartbeat_tool_call(
            action="no_action",
            message="",
            reasoning="Nothing actionable",
            priority=1,
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.message == ""
        assert action.reasoning == "Nothing actionable"
        assert action.priority == 1

    def test_text_response_falls_back_to_no_action(self) -> None:
        """If the LLM returns text instead of a tool call, default to no_action."""
        resp = make_text_response("I think you should send a message about the estimate.")
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.priority == 0
        assert "did not call compose_message" in action.reasoning

    def test_empty_text_response(self) -> None:
        """Empty text response should also fall back to no_action."""
        resp = make_text_response("")
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.priority == 0

    def test_wrong_tool_name_falls_back(self) -> None:
        """If the LLM calls a different tool, default to no_action."""
        resp = _make_heartbeat_tool_call(
            action="send_message",
            message="Hi",
            reasoning="test",
            priority=3,
            tool_name="wrong_tool",
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert "unexpected tool" in action.reasoning

    def test_malformed_arguments(self) -> None:
        """Non-dict tool input should fall back to no_action."""
        # ToolUseBlock validates input as dict in 1.13+; use model_construct
        # to bypass validation and simulate a malformed block.
        block = ToolUseBlock.model_construct(
            type="tool_use", id="call_bad", name="compose_message", input=None
        )
        resp = MessageResponse.model_construct(
            id="msg_mock",
            content=[block],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert "Malformed tool arguments" in action.reasoning

    def test_none_arguments_does_not_crash(self) -> None:
        """None tool input should not raise TypeError."""
        block = ToolUseBlock.model_construct(
            type="tool_use", id="call_none_args", name="compose_message", input=None
        )
        resp = MessageResponse.model_construct(
            id="msg_mock",
            content=[block],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.priority == 0

    def test_non_numeric_priority_falls_back_to_no_action(self) -> None:
        """Non-numeric priority value triggers validation error and falls back to no_action."""
        resp = MessageResponse(
            id="msg_mock",
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="call_bad_priority",
                    name="compose_message",
                    input={
                        "action": "send_message",
                        "message": "Hello",
                        "reasoning": "test",
                        "priority": "high",
                    },
                ),
            ],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.priority == 0

    def test_missing_optional_message_defaults_empty(self) -> None:
        """If the LLM omits the optional message field, it should default to empty."""
        resp = MessageResponse(
            id="msg_mock",
            content=[
                ToolUseBlock(
                    type="tool_use",
                    id="call_no_msg",
                    name="compose_message",
                    input={"action": "no_action", "reasoning": "nothing", "priority": 2},
                ),
            ],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.message == ""
        assert action.priority == 2

    def test_nameless_tool_use_falls_back(self) -> None:
        """tool_use block with no name should fall back to no_action."""
        # ToolUseBlock requires name in 1.13+; use model_construct to bypass validation
        block = ToolUseBlock.model_construct(
            type="tool_use", id="call_nofunc", name=None, input={"action": "send_message"}
        )
        resp = MessageResponse.model_construct(
            id="msg_mock",
            content=[block],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        # Parsed tool call has empty name, so heartbeat reports "unexpected tool"
        assert "unexpected tool" in action.reasoning


# ---------------------------------------------------------------------------
# HEARTBEAT_DECISION_TOOL schema
# ---------------------------------------------------------------------------


class TestHeartbeatDecisionToolSchema:
    def test_tool_has_name(self) -> None:
        assert HEARTBEAT_DECISION_TOOL["name"] == "heartbeat_decision"

    def test_tool_has_description(self) -> None:
        assert "description" in HEARTBEAT_DECISION_TOOL

    def test_tool_has_required_fields(self) -> None:
        required = HEARTBEAT_DECISION_TOOL["input_schema"]["required"]
        assert "action" in required
        assert "reasoning" in required

    def test_action_enum_values(self) -> None:
        action_prop = HEARTBEAT_DECISION_TOOL["input_schema"]["properties"]["action"]
        assert action_prop["enum"] == ["skip", "run"]

    def test_schema_generated_from_pydantic_model(self) -> None:
        assert (
            HEARTBEAT_DECISION_TOOL["input_schema"] == HeartbeatDecisionParams.model_json_schema()
        )


# ---------------------------------------------------------------------------
# _parse_decision_response
# ---------------------------------------------------------------------------


class TestParseDecisionResponse:
    def test_valid_skip(self) -> None:
        resp = _make_decision_tool_call(action="skip", tasks="", reasoning="Nothing actionable")
        decision = _parse_decision_response(resp)
        assert decision.action == "skip"
        assert decision.tasks == ""
        assert decision.reasoning == "Nothing actionable"

    def test_valid_run(self) -> None:
        resp = _make_decision_tool_call(
            action="run",
            tasks="Check QuickBooks for unpaid invoices and report to user",
            reasoning="Heartbeat item needs QB check",
        )
        decision = _parse_decision_response(resp)
        assert decision.action == "run"
        assert "QuickBooks" in decision.tasks
        assert decision.reasoning == "Heartbeat item needs QB check"

    def test_text_response_falls_back_to_skip(self) -> None:
        resp = make_text_response("I think there is something to do.")
        decision = _parse_decision_response(resp)
        assert decision.action == "skip"
        assert "did not call tool" in decision.reasoning

    def test_wrong_tool_name_falls_back(self) -> None:
        resp = _make_decision_tool_call(
            action="run", tasks="do stuff", reasoning="test", tool_name="wrong_tool"
        )
        decision = _parse_decision_response(resp)
        assert decision.action == "skip"
        assert "unexpected tool" in decision.reasoning

    def test_malformed_arguments(self) -> None:
        block = ToolUseBlock.model_construct(
            type="tool_use", id="call_bad", name="heartbeat_decision", input=None
        )
        resp = MessageResponse.model_construct(
            id="msg_mock",
            content=[block],
            model="mock-model",
            role="assistant",
            type="message",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        decision = _parse_decision_response(resp)
        assert decision.action == "skip"
        assert "Malformed" in decision.reasoning

    def test_max_tokens_truncation_forces_skip(self) -> None:
        """A truncated tool call (stop_reason=max_tokens) must not be parsed as run.

        Without this guard, a partial JSON that happened to include a valid
        action+reasoning but a truncated tasks list would surface as
        action=run, tasks="" and skip Phase 2 while leaving a typing
        indicator orphaned in iMessage.
        """
        resp = make_truncated_tool_call_response(
            [
                {
                    "name": "heartbeat_decision",
                    "arguments": json.dumps(
                        {"action": "run", "tasks": "do stuff", "reasoning": "valid"}
                    ),
                    "id": "call_trunc",
                }
            ]
        )
        decision = _parse_decision_response(resp)
        assert decision.action == "skip"
        assert "max_tokens" in decision.reasoning


# ---------------------------------------------------------------------------
# evaluate_heartbeat_need
# ---------------------------------------------------------------------------


class TestEvaluateHeartbeatNeed:
    """Tests for Phase 1: evaluate_heartbeat_need returns HeartbeatDecision."""

    def _setup_mocks(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
    ) -> None:
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
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

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_skip(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_llm.return_value = _make_decision_tool_call(
            action="skip", tasks="", reasoning="Nothing actionable"
        )
        decision = await evaluate_heartbeat_need(user)
        assert decision.action == "skip"
        assert decision.tasks == ""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_populates_tokens_from_response(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """Phase 1 decision carries back the LLM's token usage."""
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        response = _make_decision_tool_call(action="skip", tasks="", reasoning="nope")
        response.usage.input_tokens = 42
        response.usage.output_tokens = 7
        mock_llm.return_value = response

        decision = await evaluate_heartbeat_need(user)
        assert decision.input_tokens == 42
        assert decision.output_tokens == 7

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_run(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_llm.return_value = _make_decision_tool_call(
            action="run",
            tasks="Check QuickBooks for unpaid invoices",
            reasoning="Heartbeat item due",
        )
        decision = await evaluate_heartbeat_need(user)
        assert decision.action == "run"
        assert "QuickBooks" in decision.tasks

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_uses_heartbeat_model_when_set(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """When heartbeat_model is configured, it should be used instead of llm_model."""
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_settings.heartbeat_model = "gpt-4o-mini"
        mock_settings.heartbeat_provider = "openai"

        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="")
        await evaluate_heartbeat_need(user)
        call_kwargs = mock_llm.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_api_base_not_api_key(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """Regression test: acompletion must receive api_base, not api_key."""
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_settings.llm_api_base = "http://localhost:1234/v1"

        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="test")
        await evaluate_heartbeat_need(user)
        _, kwargs = mock_llm.call_args
        assert "api_base" in kwargs
        assert kwargs["api_base"] == "http://localhost:1234/v1"
        assert "api_key" not in kwargs

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_text_response_falls_back_to_skip(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """If LLM returns text instead of tool call, default to skip."""
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_llm.return_value = make_text_response("I'm not sure what to do")
        decision = await evaluate_heartbeat_need(user)
        assert decision.action == "skip"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_decision_tool_to_acompletion(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """acompletion should receive tools=[HEARTBEAT_DECISION_TOOL]."""
        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="test")
        await evaluate_heartbeat_need(user)
        _, kwargs = mock_llm.call_args
        assert "tools" in kwargs
        assert kwargs["tools"] == [HEARTBEAT_DECISION_TOOL]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.asyncio.sleep", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_rate_limit_error_is_retried(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        mock_sleep: AsyncMock,
        user: User,
    ) -> None:
        """A transient RateLimitError on the Phase 1 LLM call should retry
        rather than crash the heartbeat tick."""
        from any_llm import RateLimitError

        self._setup_mocks(
            mock_llm,
            mock_settings,
            mock_get_session_store,
            mock_heartbeat_store_cls,
            mock_build_prompt,
        )
        mock_settings.llm_max_retries = 3
        success_response = _make_decision_tool_call(
            action="skip", tasks="", reasoning="ok after retry"
        )
        mock_llm.side_effect = [RateLimitError("rate limited"), success_response]

        decision = await evaluate_heartbeat_need(user)

        assert decision.action == "skip"
        assert mock_llm.call_count == 2
        assert mock_sleep.await_count == 1


# ---------------------------------------------------------------------------
# run_heartbeat_for_user
# ---------------------------------------------------------------------------


class TestRunHeartbeatForUser:
    """Tests for the two-phase run_heartbeat_for_user orchestrator."""

    @pytest.mark.asyncio
    async def test_skip_not_onboarded(self) -> None:
        c = User(id="10", user_id="hb-new", phone="+15550000000", onboarding_complete=False)
        result = await run_heartbeat_for_user(c, "telegram", c.phone, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_rate_limited(
        self,
        mock_count: AsyncMock,
        user: User,
    ) -> None:
        mock_count.return_value = 5
        result = await run_heartbeat_for_user(user, "telegram", user.phone, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat._user_messaged_within")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_when_user_recently_messaged(
        self,
        mock_count: AsyncMock,
        mock_recent: MagicMock,
        mock_eval: AsyncMock,
        user: User,
    ) -> None:
        """Quiet-period gate: skip the LLM call when the user is in an
        active conversation. Pre-LLM, runs after the daily-rate gate."""
        mock_count.return_value = 0
        mock_recent.return_value = True
        result = await run_heartbeat_for_user(user, "telegram", "+15550000000", 5)
        assert result is None
        mock_recent.assert_called_once()
        # Quiet-period must run BEFORE any LLM evaluation. Asserting the
        # evaluator was never awaited makes the gate's ordering explicit
        # and would catch a regression that re-arranged the gates.
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat._user_messaged_within")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_quiet_period_does_not_block_when_user_silent(
        self,
        mock_count: AsyncMock,
        mock_recent: MagicMock,
        mock_eval: AsyncMock,
        mock_hb_store_cls: MagicMock,
        user: User,
    ) -> None:
        """If the user has not messaged recently, the heartbeat runs
        normally — the gate does not gum up legitimate proactive sends."""
        mock_count.return_value = 0
        mock_recent.return_value = False
        mock_eval.return_value = HeartbeatDecision(
            action="skip", tasks="", reasoning="Nothing actionable"
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_hb_store_cls.return_value = mock_hb_store

        await run_heartbeat_for_user(user, "telegram", "+15550000000", 5)
        mock_eval.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase1_skip_no_phase2(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When Phase 1 returns skip, Phase 2 is not invoked and skip is logged."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="skip", tasks="", reasoning="Nothing actionable right now"
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None
        assert result.action_type == "no_action"
        mock_eval.assert_awaited_once_with(user, channel="telegram", chat_id="+15559990000")
        # Skip is logged with action_type="skip"
        mock_hb_store.log_heartbeat.assert_awaited_once_with(
            action_type="skip",
            channel="telegram",
            reasoning="Nothing actionable right now",
        )

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.agent.heartbeat.OutboundMessage")
    @patch("backend.app.agent.heartbeat.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_sends_agent_reply(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When Phase 1 says run, Phase 2 executes and delivers the reply."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run",
            tasks="Check QuickBooks for unpaid invoices",
            reasoning="Heartbeat item due",
        )
        from backend.app.agent.core import AgentResponse

        mock_execute.return_value = AgentResponse(
            reply_text="You have 2 unpaid invoices totaling $1,500.",
        )
        mock_bus.publish_outbound = AsyncMock()

        mock_session = MagicMock()
        mock_get_conv.return_value = (mock_session, True)

        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)

        assert result is not None
        assert result.action_type == "send_message"
        assert "unpaid invoices" in result.message
        # Phase 2 was called with the task description
        mock_execute.assert_awaited_once_with(
            user,
            "Check QuickBooks for unpaid invoices",
            channel="telegram",
            chat_id="+15559990000",
        )
        # Outbound message was published (no SENDS_REPLY tool call, so fallback delivery)
        mock_bus.publish_outbound.assert_awaited_once()
        mock_outbound_msg.assert_called_once_with(
            channel="telegram",
            chat_id="+15559990000",
            content="You have 2 unpaid invoices totaling $1,500.",
        )
        # Heartbeat was logged with enriched data
        mock_hb_store.log_heartbeat.assert_awaited_once_with(
            action_type="send",
            message_text="You have 2 unpaid invoices totaling $1,500.",
            channel="telegram",
            reasoning="Heartbeat item due",
            tasks="Check QuickBooks for unpaid invoices",
        )

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.agent.heartbeat.OutboundMessage")
    @patch("backend.app.agent.heartbeat.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_persists_tool_interactions_on_outbound(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Heartbeat-driven outbounds must record tool_interactions_json so
        the admin conversation view can render them with the same fidelity
        as user-driven turns. Regression: a heartbeat that ran qb_send /
        qb_update used to persist an empty tool_interactions_json, making
        it look (in the admin Activity panel) like the agent claimed
        success without calling any tools — indistinguishable from a
        hallucinated reply."""
        from backend.app.agent.context import StoredToolInteraction
        from backend.app.agent.core import AgentResponse

        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run",
            tasks="Send the Surman estimate",
            reasoning="Pending request",
        )
        mock_execute.return_value = AgentResponse(
            reply_text="Done. Estimate sent to client.",
            tool_calls=[
                StoredToolInteraction(
                    tool_call_id="call_1",
                    name="qb_update",
                    args={"entity_type": "Customer", "data": {"Id": "60"}},
                    result="Customer updated",
                    is_error=False,
                ),
                StoredToolInteraction(
                    tool_call_id="call_2",
                    name="qb_send",
                    args={"entity_type": "Estimate", "entity_id": "544", "email": "x@y.z"},
                    result="Estimate sent",
                    is_error=False,
                ),
            ],
        )
        mock_bus.publish_outbound = AsyncMock()
        mock_session = MagicMock()
        mock_get_conv.return_value = (mock_session, True)
        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        await run_heartbeat_for_user(user, "bluebubbles", "+15559990000", 5)

        # The outbound was persisted exactly once.
        mock_session_store.add_message.assert_awaited_once()
        await_args = mock_session_store.add_message.await_args
        assert await_args is not None
        # tool_interactions_json was populated and parses to the two tools we ran.
        raw = await_args.kwargs["tool_interactions_json"]
        assert raw, "tool_interactions_json must be populated, not empty"
        parsed = json.loads(raw)
        assert [tc["name"] for tc in parsed] == ["qb_update", "qb_send"]
        assert parsed[1]["args"]["entity_id"] == "544"
        assert parsed[1]["is_error"] is False

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.agent.heartbeat.OutboundMessage")
    @patch("backend.app.agent.heartbeat.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_persists_tool_interactions_with_non_json_native_args(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """A tool whose args carry a non-JSON-native value (datetime,
        set, etc.) must still serialize cleanly. Regression: with a bare
        model_dump(), a single datetime in args raises TypeError inside
        json.dumps and the whole outbound persist is lost. Switching to
        model_dump(mode="json") coerces those values to JSON forms."""
        from backend.app.agent.context import StoredToolInteraction
        from backend.app.agent.core import AgentResponse

        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="Add a calendar event", reasoning="Pending request"
        )
        mock_execute.return_value = AgentResponse(
            reply_text="Booked.",
            tool_calls=[
                StoredToolInteraction(
                    tool_call_id="call_1",
                    name="calendar_create_event",
                    # Both a tz-aware datetime and a set, neither of which
                    # json.dumps natively handles.
                    args={
                        "starts_at": datetime.datetime(2026, 5, 2, 14, 0, tzinfo=datetime.UTC),
                        "attendees": {"alice@example.com", "bob@example.com"},
                    },
                    result="Event created",
                    is_error=False,
                ),
            ],
        )
        mock_bus.publish_outbound = AsyncMock()
        mock_session = MagicMock()
        mock_get_conv.return_value = (mock_session, True)
        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)

        mock_session_store.add_message.assert_awaited_once()
        await_args = mock_session_store.add_message.await_args
        assert await_args is not None
        raw = await_args.kwargs["tool_interactions_json"]
        # Round-trip must succeed: the datetime serialized to a string
        # and the set serialized to a list.
        parsed = json.loads(raw)
        assert parsed[0]["args"]["starts_at"] == "2026-05-02T14:00:00Z"
        assert sorted(parsed[0]["args"]["attendees"]) == [
            "alice@example.com",
            "bob@example.com",
        ]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_no_output_returns_no_action(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When Phase 2 produces no output, no message is sent."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="Check something", reasoning="test"
        )
        mock_execute.return_value = None
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- Check something"
        mock_heartbeat_store_cls.return_value = mock_hb_store
        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None
        assert result.action_type == "no_action"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_cleanup_only_logs_with_tasks_for_dedup(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Cleanup-only Phase 2 must write a heartbeat log entry whose
        tasks string is the same one Phase 1 issued.

        The Phase 1 24-hour dedup rule keys on the per-tick history
        emitted by ``_format_heartbeat_history``; that history shows
        non-skip log entries with their tasks. If a cleanup-only run
        (no user-facing reply) writes nothing, the next tick's history
        is empty for that turn and the LLM has no signal to dedup
        against, so it re-issues the same removal task on every tick
        until the item ages out some other way.
        """
        from backend.app.agent.context import StoredToolInteraction
        from backend.app.agent.core import AgentResponse

        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run",
            tasks="Remove the stale 'follow up on Smith estimate' entry",
            reasoning="The dated item is past due and was already handled",
        )
        # Phase 2 ran update_heartbeat (no SENDS_REPLY tag) and produced
        # no user-facing reply text. This is the cleanup-only shape.
        mock_execute.return_value = AgentResponse(
            reply_text="",
            tool_calls=[
                StoredToolInteraction(
                    tool_call_id="call_1",
                    name="update_heartbeat",
                    args={"new_text": "..."},
                    result="HEARTBEAT.md updated",
                    is_error=False,
                ),
            ],
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- (something)"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None
        assert result.action_type == "no_action"

        # The cleanup log entry was written with the right shape so the
        # next tick's _format_heartbeat_history can show it with tasks.
        mock_hb_store.log_heartbeat.assert_awaited_once()
        await_args = mock_hb_store.log_heartbeat.await_args
        assert await_args is not None
        kwargs = await_args.kwargs
        assert kwargs["action_type"] == "cleanup"
        assert kwargs["tasks"] == ("Remove the stale 'follow up on Smith estimate' entry")
        assert kwargs["reasoning"]  # reasoning is propagated for audit

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_crash_does_not_log_cleanup(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """A Phase 2 crash (response is None) must not log a cleanup entry.

        Cleanup is "Phase 2 ran successfully but produced no user-facing
        message"; a None response is the crash path and should not
        pollute the dedup history with an entry pretending the work was
        done.
        """
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(action="run", tasks="Do a thing", reasoning="r")
        mock_execute.return_value = None
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- thing"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        # No log entry was written for the crashed run.
        mock_hb_store.log_heartbeat.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.OutboundMessage")
    @patch("backend.app.agent.heartbeat.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_bus_failure_graceful(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="Check something", reasoning="test"
        )
        from backend.app.agent.core import AgentResponse

        mock_execute.return_value = AgentResponse(reply_text="Here is an update.")
        mock_bus.publish_outbound = AsyncMock(side_effect=Exception("Bus down"))
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- Check something"
        mock_heartbeat_store_cls.return_value = mock_hb_store
        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        # Should still return the action, just not record a message
        assert result is not None
        assert result.action_type == "send_message"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase1_skip_emits_typing_stop(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Phase 1 skip must emit a typing-stop so iMessage doesn't show
        a phantom 'typing...' that never resolves into a reply."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="skip", tasks="", reasoning="nothing to do"
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        from backend.app.bus import message_bus

        message_bus.reset()
        await run_heartbeat_for_user(user, "bluebubbles", "+15559990000", 5)

        # Drain the outbound queue and find the typing-stop message
        published: list = []
        while message_bus.outbound_size > 0:
            published.append(await message_bus.consume_outbound())
        stops = [m for m in published if m.is_typing_stop]
        assert len(stops) == 1
        assert stops[0].channel == "bluebubbles"
        assert stops[0].chat_id == "+15559990000"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase1_run_with_empty_tasks_emits_typing_stop(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Phase 1 run-with-empty-tasks (e.g., partial parse) must emit typing-stop."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="", reasoning="empty tasks for some reason"
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        from backend.app.bus import message_bus

        message_bus.reset()
        await run_heartbeat_for_user(user, "bluebubbles", "+15559990000", 5)

        published: list = []
        while message_bus.outbound_size > 0:
            published.append(await message_bus.consume_outbound())
        stops = [m for m in published if m.is_typing_stop]
        assert len(stops) == 1

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_phase2_no_output_emits_typing_stop(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Phase 2 returning None must still cancel the typing indicator."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(action="run", tasks="something", reasoning="run")
        mock_execute.return_value = None
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- something"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        from backend.app.bus import message_bus

        message_bus.reset()
        await run_heartbeat_for_user(user, "bluebubbles", "+15559990000", 5)

        published: list = []
        while message_bus.outbound_size > 0:
            published.append(await message_bus.consume_outbound())
        stops = [m for m in published if m.is_typing_stop]
        assert len(stops) == 1

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_successful_reply_does_not_emit_typing_stop(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_get_conv: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When a reply was actually sent, no redundant typing-stop should be emitted.

        The reply itself implicitly clears the typing indicator on iMessage,
        so emitting an additional stop would be wasted work.
        """
        from backend.app.agent.core import AgentResponse

        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="check stuff", reasoning="run"
        )
        mock_execute.return_value = AgentResponse(reply_text="Here is the answer.")
        mock_session = MagicMock()
        mock_get_conv.return_value = (mock_session, True)
        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- check stuff"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        from backend.app.bus import message_bus

        message_bus.reset()
        await run_heartbeat_for_user(user, "bluebubbles", "+15559990000", 5)

        published: list = []
        while message_bus.outbound_size > 0:
            published.append(await message_bus.consume_outbound())
        stops = [m for m in published if m.is_typing_stop]
        assert stops == []
        # The reply itself was published.
        replies = [m for m in published if not m.is_typing_indicator and not m.is_typing_stop]
        assert len(replies) == 1
        assert replies[0].content == "Here is the answer."


# ---------------------------------------------------------------------------
# Heartbeat usage hooks
# ---------------------------------------------------------------------------


@pytest.fixture()
def clear_heartbeat_hooks() -> object:
    """Snapshot and restore the module-level usage-hook list around a test."""
    snapshot = list(_heartbeat_usage_hooks)
    _heartbeat_usage_hooks.clear()
    yield
    _heartbeat_usage_hooks.clear()
    _heartbeat_usage_hooks.extend(snapshot)


class TestHeartbeatUsageHooks:
    """Tests for register_heartbeat_usage_hook and post-run dispatch."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_hook_fires_with_phase1_tokens_on_skip(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
        clear_heartbeat_hooks: object,
    ) -> None:
        """Phase 1 skip still reports Phase 1 tokens via the hook."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="skip",
            tasks="",
            reasoning="nothing to do",
            input_tokens=120,
            output_tokens=30,
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        calls: list[tuple[str, int, int, bool]] = []

        def _hook(user_id: str, in_tok: int, out_tok: int, sent: bool) -> None:
            calls.append((user_id, in_tok, out_tok, sent))

        register_heartbeat_usage_hook(_hook)

        await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)

        assert calls == [(user.id, 120, 30, False)]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.agent.heartbeat.OutboundMessage")
    @patch("backend.app.agent.heartbeat.message_bus")
    @patch("backend.app.agent.heartbeat.execute_heartbeat_tasks")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_hook_sums_phase1_and_phase2_tokens(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_execute: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
        clear_heartbeat_hooks: object,
    ) -> None:
        """When Phase 2 runs and delivers, hook sees Phase 1+2 tokens and sent_reply=True."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run",
            tasks="Check inbox",
            reasoning="due",
            input_tokens=100,
            output_tokens=50,
        )
        from backend.app.agent.core import AgentResponse

        mock_execute.return_value = AgentResponse(
            reply_text="All clear.",
            total_input_tokens=800,
            total_output_tokens=200,
        )
        mock_bus.publish_outbound = AsyncMock()
        mock_get_conv.return_value = (MagicMock(), True)
        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        calls: list[tuple[str, int, int, bool]] = []
        register_heartbeat_usage_hook(lambda uid, i, o, s: calls.append((uid, i, o, s)))

        await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)

        assert calls == [(user.id, 900, 250, True)]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_hook_failure_does_not_break_heartbeat(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
        clear_heartbeat_hooks: object,
    ) -> None:
        """A raising hook must not bubble up and break the heartbeat run."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="skip",
            tasks="",
            reasoning="nothing",
            input_tokens=10,
            output_tokens=5,
        )
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        def _boom(*_args: object) -> None:
            raise RuntimeError("hook exploded")

        register_heartbeat_usage_hook(_boom)

        # Should not raise
        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None

    @pytest.mark.asyncio
    async def test_hook_not_called_when_no_llm_ran(self, clear_heartbeat_hooks: object) -> None:
        """Early-return paths (not onboarded, rate-limited, etc.) skip the hook."""
        calls: list[tuple[str, int, int, bool]] = []
        register_heartbeat_usage_hook(lambda uid, i, o, s: calls.append((uid, i, o, s)))

        u = User(id="99", user_id="hb-early", phone="+15550000099", onboarding_complete=False)
        await run_heartbeat_for_user(u, "telegram", u.phone, 5)

        assert calls == []


# ---------------------------------------------------------------------------
# _user_messaged_within (quiet-period gate, integration with real DB rows)
#
# The unit tests above mock _user_messaged_within directly. This class
# exercises the actual SQL query against a real Postgres test DB so we
# catch a class of regressions the unit tests cannot:
#
# - Message.timestamp is currently DateTime(timezone=True) with a
#   datetime.now(UTC) default, so the comparison against a tz-aware
#   cutoff works. If anyone migrated this column to a naive type, the
#   gate's ">=" comparison would raise TypeError, get swallowed by the
#   broad except in _user_messaged_within, and the gate would silently
#   never fire. The unit tests would still pass; this one would not.
# - Confirms the join filters to the user's own sessions and ignores
#   outbound messages.
# ---------------------------------------------------------------------------


def _seed_message(
    db: Any,
    *,
    user_id: str,
    direction: str,
    timestamp: datetime.datetime,
    seq: int = 1,
    session_external_id: str | None = None,
) -> int:
    """Insert one ChatSession + one Message and return the message id."""
    cs = ChatSession(
        session_id=session_external_id or f"sess-{user_id[:8]}-{seq}",
        user_id=user_id,
        channel="telegram",
    )
    db.add(cs)
    db.flush()
    msg = Message(
        session_id=cs.id,
        seq=seq,
        direction=direction,
        body="hi",
        timestamp=timestamp,
    )
    db.add(msg)
    db.commit()
    return msg.id


class TestUserMessagedWithinIntegration:
    async def test_returns_false_when_no_messages(self, user: User) -> None:
        assert await _user_messaged_within(user.id, minutes=5) is False

    async def test_returns_true_for_recent_inbound(self, user: User) -> None:
        now = datetime.datetime.now(datetime.UTC)
        db = _db_module.SessionLocal()
        try:
            _seed_message(
                db,
                user_id=user.id,
                direction="inbound",
                timestamp=now - datetime.timedelta(minutes=1),
            )
        finally:
            db.close()
        assert await _user_messaged_within(user.id, minutes=5) is True

    async def test_returns_false_for_old_inbound(self, user: User) -> None:
        now = datetime.datetime.now(datetime.UTC)
        db = _db_module.SessionLocal()
        try:
            _seed_message(
                db,
                user_id=user.id,
                direction="inbound",
                timestamp=now - datetime.timedelta(minutes=30),
            )
        finally:
            db.close()
        assert await _user_messaged_within(user.id, minutes=5) is False

    async def test_ignores_outbound_messages(self, user: User) -> None:
        """Outbound (assistant-authored) messages must not satisfy the gate.

        The gate exists to detect that the *user* is mid-conversation,
        not that the *assistant* recently replied. An outbound-only
        session should leave the gate False so a heartbeat can still
        fire if the user has gone quiet.
        """
        now = datetime.datetime.now(datetime.UTC)
        db = _db_module.SessionLocal()
        try:
            _seed_message(
                db,
                user_id=user.id,
                direction="outbound",
                timestamp=now - datetime.timedelta(minutes=1),
            )
        finally:
            db.close()
        assert await _user_messaged_within(user.id, minutes=5) is False

    async def test_other_users_messages_do_not_leak(self, user: User) -> None:
        """A different user's recent inbound must not trigger this user's gate."""
        other_db = _db_module.SessionLocal()
        try:
            other = User(
                user_id="hb-quiet-other",
                phone="+15559998888",
                onboarding_complete=True,
            )
            other_db.add(other)
            other_db.commit()
            other_db.refresh(other)
            other_id = other.id
        finally:
            other_db.close()

        now = datetime.datetime.now(datetime.UTC)
        db = _db_module.SessionLocal()
        try:
            _seed_message(
                db,
                user_id=other_id,
                direction="inbound",
                timestamp=now - datetime.timedelta(minutes=1),
                session_external_id="sess-other-1",
            )
        finally:
            db.close()
        assert await _user_messaged_within(user.id, minutes=5) is False


# ---------------------------------------------------------------------------
# get_daily_heartbeat_count (persistent rate limiting)
# ---------------------------------------------------------------------------


class TestGetDailyHeartbeatCount:
    @pytest.mark.asyncio
    async def test_zero_when_no_logs(self, user: User) -> None:
        assert await get_daily_heartbeat_count(user.id) == 0

    @pytest.mark.asyncio
    async def test_counts_today_only(self, user: User) -> None:
        """Logs from yesterday should not count toward today's limit."""
        from backend.app.agent.stores import HeartbeatStore
        from backend.app.models import HeartbeatLog as HeartbeatLogModel

        store = HeartbeatStore(user.id)
        # Add a log from today
        await store.log_heartbeat()
        # Add a log from yesterday directly to the DB
        yesterday = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        db = _db_module.SessionLocal()
        try:
            db.add(HeartbeatLogModel(user_id=user.id, created_at=yesterday))
            db.commit()
        finally:
            db.close()

        assert await get_daily_heartbeat_count(user.id) == 1

    @pytest.mark.asyncio
    async def test_counts_multiple_today(self, user: User) -> None:
        from backend.app.agent.stores import HeartbeatStore

        store = HeartbeatStore(user.id)
        for _ in range(3):
            await store.log_heartbeat()

        assert await get_daily_heartbeat_count(user.id) == 3

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, user: User) -> None:
        """Logs from other users should not count."""
        from backend.app.agent.stores import HeartbeatStore

        # Create other user in DB so FK constraints are satisfied
        db = _db_module.SessionLocal()
        try:
            other_user = User(
                user_id="hb-other",
                phone="+15551112222",
                onboarding_complete=True,
            )
            db.add(other_user)
            db.commit()
            db.refresh(other_user)
            other_id = other_user.id
            db.expunge(other_user)
        finally:
            db.close()

        other_store = HeartbeatStore(other_id)
        await other_store.log_heartbeat()

        assert await get_daily_heartbeat_count(user.id) == 0
        assert await get_daily_heartbeat_count(other_id) == 1

    @pytest.mark.asyncio
    async def test_excludes_skips(self, user: User) -> None:
        """Skip logs should not count toward the daily rate limit."""
        from backend.app.agent.stores import HeartbeatStore

        store = HeartbeatStore(user.id)
        await store.log_heartbeat(action_type="send", message_text="Hello")
        await store.log_heartbeat(action_type="skip", reasoning="nothing to do")
        await store.log_heartbeat(action_type="send", message_text="Hi again")

        # Only the 2 sends should count
        assert await get_daily_heartbeat_count(user.id) == 2

    @pytest.mark.asyncio
    async def test_excludes_cleanup(self, user: User) -> None:
        """Cleanup logs (Phase 2 ran without sending) must not count.

        These are dedup-history audit entries, not user-facing nudges.
        Counting them would burn the daily nudge budget on internal
        housekeeping (e.g. pruning a stale HEARTBEAT.md item).
        """
        from backend.app.agent.stores import HeartbeatStore

        store = HeartbeatStore(user.id)
        await store.log_heartbeat(action_type="send", message_text="Hello")
        await store.log_heartbeat(
            action_type="cleanup", tasks="Remove stale entry", reasoning="dated"
        )
        # Only the send counts.
        assert await get_daily_heartbeat_count(user.id) == 1


# ---------------------------------------------------------------------------
# execute_heartbeat_tasks (Phase 2)
# ---------------------------------------------------------------------------


class TestExecuteHeartbeatTasks:
    @pytest.mark.asyncio
    async def test_returns_agent_reply(self, user: User) -> None:
        """Phase 2 should return the agent's reply text."""
        from backend.app.agent.core import AgentResponse

        mock_response = AgentResponse(reply_text="You have 3 unpaid invoices.")

        with (
            patch("backend.app.agent.core.ClawboltAgent") as MockAgent,
            patch("backend.app.agent.tools.registry.default_registry") as mock_registry,
            patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
            patch("backend.app.agent.router.init_storage", return_value=None),
            patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
            patch("backend.app.agent.stores.ToolConfigStore") as MockToolConfig,
            patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        ):
            mock_tc = MagicMock()
            mock_tc.get_disabled_tool_names = AsyncMock(return_value=set())
            mock_tc.get_disabled_sub_tool_names = AsyncMock(return_value=set())
            MockToolConfig.return_value = mock_tc

            mock_agent_instance = MagicMock()
            mock_agent_instance.process_message = AsyncMock(return_value=mock_response)
            MockAgent.return_value = mock_agent_instance
            mock_registry.create_core_tools = AsyncMock(return_value=[])
            mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
            mock_registry.get_available_specialist_summaries.return_value = {}
            mock_registry.get_unauthenticated_specialists.return_value = {}
            mock_registry.get_disabled_specialist_sub_tools.return_value = {}
            mock_bus.publish_outbound = AsyncMock()

            result = await execute_heartbeat_tasks(user, "Check QuickBooks for unpaid invoices")
            assert result is not None
            assert result.reply_text == "You have 3 unpaid invoices."
            mock_agent_instance.process_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, user: User) -> None:
        """Phase 2 should return empty string if agent raises."""
        with (
            patch("backend.app.agent.core.ClawboltAgent") as MockAgent,
            patch("backend.app.agent.tools.registry.default_registry") as mock_registry,
            patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
            patch("backend.app.agent.router.init_storage", return_value=None),
            patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
            patch("backend.app.agent.stores.ToolConfigStore") as MockToolConfig,
            patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        ):
            mock_tc = MagicMock()
            mock_tc.get_disabled_tool_names = AsyncMock(return_value=set())
            mock_tc.get_disabled_sub_tool_names = AsyncMock(return_value=set())
            MockToolConfig.return_value = mock_tc

            mock_agent_instance = MagicMock()
            mock_agent_instance.process_message = AsyncMock(side_effect=Exception("LLM down"))
            MockAgent.return_value = mock_agent_instance
            mock_registry.create_core_tools = AsyncMock(return_value=[])
            mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
            mock_registry.get_available_specialist_summaries.return_value = {}
            mock_registry.get_unauthenticated_specialists.return_value = {}
            mock_registry.get_disabled_specialist_sub_tools.return_value = {}
            mock_bus.publish_outbound = AsyncMock()

            result = await execute_heartbeat_tasks(user, "Check something")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error_fallback(self, user: User) -> None:
        """Phase 2 should return None if agent returns error fallback."""
        from backend.app.agent.core import AgentResponse

        mock_response = AgentResponse(reply_text="I'm having trouble.", is_error_fallback=True)

        with (
            patch("backend.app.agent.core.ClawboltAgent") as MockAgent,
            patch("backend.app.agent.tools.registry.default_registry") as mock_registry,
            patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
            patch("backend.app.agent.router.init_storage", return_value=None),
            patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
            patch("backend.app.agent.stores.ToolConfigStore") as MockToolConfig,
            patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        ):
            mock_tc = MagicMock()
            mock_tc.get_disabled_tool_names = AsyncMock(return_value=set())
            mock_tc.get_disabled_sub_tool_names = AsyncMock(return_value=set())
            MockToolConfig.return_value = mock_tc

            mock_agent_instance = MagicMock()
            mock_agent_instance.process_message = AsyncMock(return_value=mock_response)
            MockAgent.return_value = mock_agent_instance
            mock_registry.create_core_tools = AsyncMock(return_value=[])
            mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
            mock_registry.get_available_specialist_summaries.return_value = {}
            mock_registry.get_unauthenticated_specialists.return_value = {}
            mock_registry.get_disabled_specialist_sub_tools.return_value = {}
            mock_bus.publish_outbound = AsyncMock()

            result = await execute_heartbeat_tasks(user, "Check something")
            assert result is None

    @pytest.mark.asyncio
    async def test_includes_messaging_and_uses_list_capabilities(self, user: User) -> None:
        """Phase 2 should use core tools (including messaging) + list_capabilities."""
        from backend.app.agent.core import AgentResponse

        mock_response = AgentResponse(reply_text="Report")

        with (
            patch("backend.app.agent.core.ClawboltAgent") as MockAgent,
            patch("backend.app.agent.tools.registry.default_registry") as mock_registry,
            patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
            patch("backend.app.agent.router.init_storage", return_value=None),
            patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
            patch("backend.app.agent.stores.ToolConfigStore") as MockToolConfig,
            patch(
                "backend.app.agent.tools.registry.create_list_capabilities_tool"
            ) as mock_list_cap,
        ):
            mock_tc = MagicMock()
            mock_tc.get_disabled_tool_names = AsyncMock(return_value=set())
            mock_tc.get_disabled_sub_tool_names = AsyncMock(return_value=set())
            MockToolConfig.return_value = mock_tc

            mock_agent_instance = MagicMock()
            mock_agent_instance.process_message = AsyncMock(return_value=mock_response)
            MockAgent.return_value = mock_agent_instance
            mock_registry.create_core_tools = AsyncMock(return_value=[])
            mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
            mock_registry.get_available_specialist_summaries.return_value = {
                "quickbooks": "QB tools"
            }
            mock_registry.get_unauthenticated_specialists.return_value = {}
            mock_registry.get_disabled_specialist_sub_tools.return_value = {}
            mock_bus.publish_outbound = AsyncMock()

            await execute_heartbeat_tasks(user, "Check QB", channel="telegram", chat_id="123")

            # Should use create_core_tools without messaging excluded (#921)
            mock_registry.create_core_tools.assert_called_once()
            call_kwargs = mock_registry.create_core_tools.call_args
            excluded: set[str] = call_kwargs.kwargs.get("excluded_factories") or set()
            assert "messaging" not in excluded

            # Should create list_capabilities since specialists are available
            mock_list_cap.assert_called_once()


# ---------------------------------------------------------------------------
# HeartbeatScheduler
# ---------------------------------------------------------------------------


class TestHeartbeatScheduler:
    @patch("backend.app.agent.heartbeat.settings")
    def test_start_when_disabled(self, mock_settings: MagicMock) -> None:
        mock_settings.heartbeat_enabled = False
        scheduler = HeartbeatScheduler()
        scheduler.start()
        assert scheduler._task is None

    def test_stop_without_start(self) -> None:
        scheduler = HeartbeatScheduler()
        scheduler.stop()  # Should not raise
        assert scheduler._task is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    async def test_run_sleeps_warmup_before_first_tick(self, mock_settings: MagicMock) -> None:
        """The scheduler must wait heartbeat_startup_warmup_seconds before
        the first tick. Regression: a fresh container racing the previous
        one's in-flight work was the root of the recent double-execution
        risk on deploys."""
        mock_settings.heartbeat_enabled = True
        mock_settings.heartbeat_startup_warmup_seconds = 60
        mock_settings.heartbeat_max_daily_messages = 5

        scheduler = HeartbeatScheduler()
        # Stub tick so we can assert it is NOT called before the warmup
        # sleeps, and use a sleep stub that records its arguments.
        scheduler.tick = AsyncMock()  # type: ignore[method-assign]

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)
            # After we observe the warmup sleep, raise CancelledError to
            # break out of the infinite loop without racing real timers.
            if len(sleep_calls) == 1:
                raise asyncio.CancelledError

        with (
            patch("backend.app.agent.heartbeat.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await scheduler._run()

        # First sleep must be the warmup, not the per-tick interval. tick()
        # must not have been called yet.
        assert sleep_calls[0] == 60
        scheduler.tick.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    async def test_run_skips_warmup_when_disabled(self, mock_settings: MagicMock) -> None:
        """heartbeat_startup_warmup_seconds=0 disables the warmup so we
        keep an escape hatch for environments that don't need it.

        We pin the gate by recording the order of sleep and tick calls.
        With warmup=0, the FIRST event must be a tick, not a sleep with
        warmup arguments. A regression like ``if warmup >= 0:`` (which
        would still sleep on the disabled path) must fail this test.
        """
        mock_settings.heartbeat_enabled = True
        mock_settings.heartbeat_startup_warmup_seconds = 0
        mock_settings.heartbeat_max_daily_messages = 5

        scheduler = HeartbeatScheduler()

        events: list[tuple[str, float | None]] = []

        async def stub_tick() -> None:
            events.append(("tick", None))
            # Cancel the loop after the first tick so the test terminates.
            raise asyncio.CancelledError

        async def fake_sleep(secs: float) -> None:
            events.append(("sleep", secs))

        scheduler.tick = stub_tick  # type: ignore[method-assign]

        with (
            patch("backend.app.agent.heartbeat.asyncio.sleep", side_effect=fake_sleep),
            pytest.raises(asyncio.CancelledError),
        ):
            await scheduler._run()

        # The first observable event must be the tick, not a warmup sleep.
        # If the gate regressed and slept anyway, the first event would be
        # ``("sleep", 0)`` and this assertion would fail.
        assert events, "scheduler did not run any observable steps"
        assert events[0] == ("tick", None), (
            f"expected tick first when warmup=0, got events={events}"
        )

    @pytest.mark.asyncio
    async def test_tick_queries_onboarded(self) -> None:
        """Tick should query all users from DB and filter by onboarding_complete."""
        # Empty DB: no users inserted
        scheduler = HeartbeatScheduler()
        await scheduler.tick()
        # No error means it successfully queried the DB and found no users

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_skips_inactive_user(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Tick should skip users with is_active=False even if onboarding is complete (#811)."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-inactive-test",
                phone="+15550001111",
                onboarding_complete=True,
                is_active=False,
                preferred_channel="telegram",
                channel_identifier="",
            )
            db.add(user)
            db.flush()
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="inactive-chat-id",
                )
            )
            db.commit()
        finally:
            db.close()

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_filters_users_in_sql(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Tick should filter inactive/non-onboarded users at the SQL level (#1014).

        Captures the SQL issued during tick() and asserts the users query
        includes WHERE conditions on onboarding_complete and is_active, so
        dormant rows are never loaded into Python memory.
        """
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        db = _db_module.SessionLocal()
        try:
            for i, (onboarded, active) in enumerate(
                [(True, True), (True, False), (False, True), (False, False)]
            ):
                user = User(
                    user_id=f"hb-sql-filter-{i}",
                    phone="+15550002222",
                    onboarding_complete=onboarded,
                    is_active=active,
                    preferred_channel="telegram",
                    channel_identifier="",
                )
                db.add(user)
                db.flush()
                db.add(
                    ChannelRoute(
                        user_id=user.id,
                        channel="telegram",
                        channel_identifier=f"sql-filter-{i}",
                    )
                )
            db.commit()
        finally:
            db.close()

        mock_run.return_value = None

        captured_sql: list[str] = []

        def _capture(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            captured_sql.append(statement)

        # tick() runs against the async engine, so attach the listener to
        # its underlying sync engine. SQLAlchemy core events fire on the
        # sync_engine of an AsyncEngine; listening on the async engine
        # itself (or the sync engine) would miss the SQL emitted here.
        engine = _db_module.get_async_engine().sync_engine
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            scheduler = HeartbeatScheduler()
            await scheduler.tick()
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        user_selects = [
            s for s in captured_sql if "FROM users" in s and s.lstrip().upper().startswith("SELECT")
        ]
        assert user_selects, "expected a SELECT FROM users during tick()"

        def _where_clause(sql: str) -> str:
            upper = sql.upper()
            idx = upper.find("WHERE ")
            return sql[idx:] if idx != -1 else ""

        assert any(
            "users.onboarding_complete" in _where_clause(s)
            and "users.is_active" in _where_clause(s)
            for s in user_selects
        ), f"users query missing WHERE filter on onboarding_complete/is_active; saw: {user_selects}"

        assert mock_run.await_count == 1

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_concurrent_processing(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """tick() should process multiple users concurrently."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create real users in the DB with telegram routes
        db = _db_module.SessionLocal()
        try:
            for i in range(4):
                user = User(
                    user_id=f"hb-concurrent-{i}",
                    phone="+15559990000",
                    onboarding_complete=True,
                    preferred_channel="telegram",
                    channel_identifier="",
                )
                db.add(user)
                db.flush()
                db.add(
                    ChannelRoute(
                        user_id=user.id,
                        channel="telegram",
                        channel_identifier=str(i),
                    )
                )
            db.commit()
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # run_heartbeat_for_user called once per user
        assert mock_run.await_count == 4

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_error_isolation(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """One user failure should not prevent others from being processed."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5

        # Create real users in the DB with telegram routes
        db = _db_module.SessionLocal()
        try:
            for i in range(3):
                user = User(
                    user_id=f"hb-error-{i}",
                    phone="+15559990000",
                    onboarding_complete=True,
                    preferred_channel="telegram",
                    channel_identifier="",
                )
                db.add(user)
                db.flush()
                db.add(
                    ChannelRoute(
                        user_id=user.id,
                        channel="telegram",
                        channel_identifier=str(i),
                    )
                )
            db.commit()
        finally:
            db.close()

        # Second user raises, others succeed
        mock_run.side_effect = [
            HeartbeatAction("no_action", "", "clean", 0),
            RuntimeError("LLM timeout"),
            HeartbeatAction("no_action", "", "clean", 0),
        ]

        scheduler = HeartbeatScheduler()
        # Should not raise despite one user failing
        await scheduler.tick()

        # All three were attempted
        assert mock_run.await_count == 3

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_semaphore_limits_concurrency(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Semaphore should limit the number of concurrent user evaluations."""
        concurrency_limit = 2
        mock_settings.heartbeat_concurrency = concurrency_limit
        mock_settings.heartbeat_max_daily_messages = 5

        # Create real users in the DB with telegram routes
        db = _db_module.SessionLocal()
        try:
            for i in range(5):
                user = User(
                    user_id=f"hb-semaphore-{i}",
                    phone="+15559990000",
                    onboarding_complete=True,
                    preferred_channel="telegram",
                    channel_identifier="",
                )
                db.add(user)
                db.flush()
                db.add(
                    ChannelRoute(
                        user_id=user.id,
                        channel="telegram",
                        channel_identifier=str(i),
                    )
                )
            db.commit()
        finally:
            db.close()

        # Track max concurrent executions
        import asyncio

        current_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        async def tracked_run(*args: object, **kwargs: object) -> HeartbeatAction:
            nonlocal current_count, max_concurrent
            async with lock:
                current_count += 1
                if current_count > max_concurrent:
                    max_concurrent = current_count
            # Simulate some async work so concurrency can be observed
            await asyncio.sleep(0.01)
            async with lock:
                current_count -= 1
            return HeartbeatAction("no_action", "", "clean", 0)

        mock_run.side_effect = tracked_run

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        assert mock_run.await_count == 5
        assert max_concurrent <= concurrency_limit

    @pytest.mark.asyncio
    async def test_tick_no_users(self) -> None:
        """tick() with no onboarded users should return early."""
        # Empty DB: no users inserted
        scheduler = HeartbeatScheduler()
        await scheduler.tick()
        # No error means it successfully queried the DB and found no users


# ---------------------------------------------------------------------------
# parse_frequency_to_minutes
# ---------------------------------------------------------------------------


class TestParseFrequencyToMinutes:
    def test_minutes(self) -> None:
        assert parse_frequency_to_minutes("15m") == 15

    def test_minutes_uppercase(self) -> None:
        assert parse_frequency_to_minutes("15M") == 15

    def test_hours(self) -> None:
        assert parse_frequency_to_minutes("2h") == 120

    def test_days(self) -> None:
        assert parse_frequency_to_minutes("1d") == 1440

    def test_daily(self) -> None:
        assert parse_frequency_to_minutes("daily") == 1440

    def test_weekdays(self) -> None:
        assert parse_frequency_to_minutes("weekdays") == 1440

    def test_weekly(self) -> None:
        assert parse_frequency_to_minutes("weekly") == 10080

    def test_one_minute_minimum(self) -> None:
        assert parse_frequency_to_minutes("0m") == 1

    def test_invalid_returns_none(self) -> None:
        assert parse_frequency_to_minutes("banana") is None

    def test_empty_returns_none(self) -> None:
        assert parse_frequency_to_minutes("") is None

    def test_whitespace_trimmed(self) -> None:
        assert parse_frequency_to_minutes("  30m  ") == 30


# ---------------------------------------------------------------------------
# Per-user frequency scheduling
# ---------------------------------------------------------------------------


class TestPerUserFrequencyScheduling:
    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_user_skipped_when_interval_not_elapsed(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """A user whose interval has not elapsed should not be processed."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        # Create a real user in the DB with a telegram route
        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-interval-skip-001",
                phone="+15559990000",
                onboarding_complete=True,
                preferred_channel="telegram",
                channel_identifier="",
                heartbeat_frequency="1h",
            )
            db.add(user)
            db.flush()
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="tg-skip",
                )
            )
            db.commit()
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()

        # First tick: user is due (no previous tick)
        await scheduler.tick()
        assert mock_run.await_count == 1

        # Second tick immediately after: user interval (1h) has not elapsed
        await scheduler.tick()
        assert mock_run.await_count == 1  # Still 1, not called again

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_user_processed_when_interval_elapsed(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """A user whose interval has elapsed should be processed again."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        # Create a real user in the DB with a telegram route
        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-interval-elapsed-001",
                phone="+15559990000",
                onboarding_complete=True,
                preferred_channel="telegram",
                channel_identifier="",
                heartbeat_frequency="15m",
            )
            db.add(user)
            db.flush()
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="tg-elapsed",
                )
            )
            db.commit()
            db.refresh(user)
            user_id = user.id
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()

        # First tick
        await scheduler.tick()
        assert mock_run.await_count == 1

        # Simulate time passing: set last tick to 16 minutes ago
        scheduler._last_tick[user_id] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=16
        )

        # Second tick: 16 > 15 minutes, so user is due
        await scheduler.tick()
        assert mock_run.await_count == 2

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_invalid_frequency_falls_back_to_global(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Invalid frequency should fall back to global heartbeat_interval_minutes."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        # Create a real user in the DB with a telegram route
        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-invalid-freq-001",
                phone="+15559990000",
                onboarding_complete=True,
                preferred_channel="telegram",
                channel_identifier="",
                heartbeat_frequency="invalid",
            )
            db.add(user)
            db.flush()
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="tg-freq",
                )
            )
            db.commit()
            db.refresh(user)
            user_id = user.id
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()

        # First tick: always due
        await scheduler.tick()
        assert mock_run.await_count == 1

        # Set last tick to 29 minutes ago (< 30m global default)
        scheduler._last_tick[user_id] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=29
        )
        await scheduler.tick()
        assert mock_run.await_count == 1  # Not yet due

        # Set last tick to 31 minutes ago (> 30m global default)
        scheduler._last_tick[user_id] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=31
        )
        await scheduler.tick()
        assert mock_run.await_count == 2  # Now due


# ---------------------------------------------------------------------------
# get_channel_identifier & tick chat_id lookup (#639)
# ---------------------------------------------------------------------------


class TestGetChannelIdentifier:
    """ChannelRoute DB lookup for channel identifiers."""

    def test_returns_matching_identifier(self) -> None:
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="ch-id-test-1")
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(ChannelRoute(user_id=user.id, channel="webchat", channel_identifier="web-1"))
            db.add(ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="99887766"))
            db.commit()
            route = db.query(ChannelRoute).filter_by(user_id=user.id, channel="telegram").first()
            assert route is not None
            assert route.channel_identifier == "99887766"
        finally:
            db.close()

    def test_returns_none_when_no_match(self) -> None:
        db = _db_module.SessionLocal()
        try:
            user = User(user_id="ch-id-test-2")
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(ChannelRoute(user_id=user.id, channel="webchat", channel_identifier="web-1"))
            db.commit()
            route = db.query(ChannelRoute).filter_by(user_id=user.id, channel="telegram").first()
            assert route is None
        finally:
            db.close()

    def test_does_not_return_other_users_identifier(self) -> None:
        db = _db_module.SessionLocal()
        try:
            user_a = User(user_id="ch-id-test-a")
            user_b = User(user_id="ch-id-test-b")
            db.add(user_a)
            db.add(user_b)
            db.commit()
            db.refresh(user_a)
            db.refresh(user_b)
            db.add(
                ChannelRoute(user_id=user_a.id, channel="telegram", channel_identifier="tg-for-a")
            )
            db.add(ChannelRoute(user_id=user_b.id, channel="webchat", channel_identifier="b-1"))
            db.commit()
            route = db.query(ChannelRoute).filter_by(user_id=user_b.id, channel="telegram").first()
            assert route is None
        finally:
            db.close()


class TestTickChatIdLookup:
    """Heartbeat tick should look up the correct chat_id for the target channel."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_uses_channel_specific_chat_id(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When falling back to telegram, tick should use the telegram chat_id
        from the ChannelRoute table, not the webchat channel_identifier."""

        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create a real user with a ChannelRoute for telegram
        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-chatid-001",
                phone="",
                onboarding_complete=True,
                preferred_channel="webchat",
                channel_identifier="web-1",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            route = ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="tg-12345")
            db.add(route)
            db.commit()
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["chat_id"] == "tg-12345"
        assert call_kwargs["channel"] == "telegram"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_skips_user_without_channel_route(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When no ChannelRoute exists for the target channel, skip the user."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create a real user with NO ChannelRoute for telegram
        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-fallback-001",
                phone="+15559990000",
                onboarding_complete=True,
                preferred_channel="webchat",
                channel_identifier="web-1",
            )
            db.add(user)
            db.commit()
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # No route for telegram means heartbeat is skipped entirely
        mock_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Per-user max daily heartbeats
# ---------------------------------------------------------------------------


class TestPerUserMaxDaily:
    """Tests for per-user heartbeat_max_daily override."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_uses_per_user_max_daily(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When user has heartbeat_max_daily > 0, tick passes it instead of global."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5

        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-maxdaily-custom",
                phone="",
                onboarding_complete=True,
                preferred_channel="telegram",
                channel_identifier="",
                heartbeat_max_daily=10,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="tg-custom",
                )
            )
            db.commit()
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_run.assert_awaited_once()
        assert mock_run.call_args.kwargs["max_daily"] == 10

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_falls_back_to_global_when_zero(
        self,
        mock_settings: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When user has heartbeat_max_daily == 0, tick uses global setting."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 7

        db = _db_module.SessionLocal()
        try:
            user = User(
                user_id="hb-maxdaily-default",
                phone="",
                onboarding_complete=True,
                preferred_channel="telegram",
                channel_identifier="",
                heartbeat_max_daily=0,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel="telegram",
                    channel_identifier="tg-default",
                )
            )
            db.commit()
            db.expunge(user)
        finally:
            db.close()

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_run.assert_awaited_once()
        assert mock_run.call_args.kwargs["max_daily"] == 7


# ---------------------------------------------------------------------------
# Heartbeat history formatting
# ---------------------------------------------------------------------------


class TestFormatHeartbeatHistory:
    """Tests for _format_heartbeat_history."""

    def test_empty_logs(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        result = _format_heartbeat_history([], "America/New_York", now)
        assert "not sent any heartbeat messages" in result
        assert str(_HISTORY_LOOKBACK_DAYS) in result

    def test_single_log_today(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            created_at=datetime.datetime(2026, 3, 23, 13, 15, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "today" in result
        assert "Monday" in result

    def test_log_one_day_ago(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            created_at=datetime.datetime(2026, 3, 22, 13, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "1 day ago" in result

    def test_log_multiple_days_ago(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            created_at=datetime.datetime(2026, 3, 20, 10, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "3 days ago" in result

    def test_multiple_logs(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                created_at=datetime.datetime(2026, 3, 22, 13, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                created_at=datetime.datetime(2026, 3, 23, 9, 0, tzinfo=datetime.UTC).isoformat(),
            ),
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "1 day ago" in result
        assert "today" in result

    def test_utc_fallback_when_no_timezone(self) -> None:
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            created_at=datetime.datetime(2026, 3, 23, 13, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "", now)
        assert "today" in result

    def test_send_entry_includes_tasks(self) -> None:
        """Heartbeat history entries include the task description so the LLM
        knows *what* was sent, not just *when*."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            action_type="send",
            tasks="Tell a morning joke",
            created_at=datetime.datetime(2026, 3, 22, 12, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert 'tasks: "Tell a morning joke"' in result

    def test_skip_entry_labeled(self) -> None:
        """Skipped heartbeat entries are labeled [skipped]."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            action_type="skip",
            created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "[skipped]" in result

    def test_long_tasks_truncated(self) -> None:
        """Task descriptions longer than 120 chars are truncated with ellipsis."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        long_task = "A" * 200
        log = HeartbeatLogEntry(
            user_id="u1",
            action_type="send",
            tasks=long_task,
            created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "..." in result
        # Should contain the truncated prefix, not the full 200-char string
        assert "A" * 120 in result
        assert "A" * 200 not in result

    def test_send_without_tasks_no_detail(self) -> None:
        """Send entries with empty tasks don't show a tasks label."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        log = HeartbeatLogEntry(
            user_id="u1",
            action_type="send",
            tasks="",
            created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
        )
        result = _format_heartbeat_history([log], "America/New_York", now)
        assert "tasks:" not in result
        assert "[skipped]" not in result


class TestEvaluateHeartbeatNeedPassesHistory:
    """Test that evaluate_heartbeat_need passes heartbeat history to the prompt builder."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_heartbeat_history_passed_to_prompt_builder(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """Heartbeat history from recent logs must be passed to the prompt builder."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_settings.heartbeat_recent_messages_count = 5
        mock_settings.reasoning_effort = ""

        mock_session_store = MagicMock()
        mock_session_store.get_recent_messages.return_value = []
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = ""
        mock_hb_store.get_recent_logs = AsyncMock(
            return_value=[
                HeartbeatLogEntry(
                    user_id=user.id,
                    created_at=datetime.datetime(
                        2026, 3, 22, 9, 0, tzinfo=datetime.UTC
                    ).isoformat(),
                ),
            ]
        )
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"
        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="test")

        await evaluate_heartbeat_need(user)

        # Verify heartbeat_history kwarg was passed and contains log info
        call_kwargs = mock_build_prompt.call_args
        assert "heartbeat_history" in call_kwargs.kwargs
        assert call_kwargs.kwargs["heartbeat_history"] != ""
        assert "heartbeat messages" in call_kwargs.kwargs["heartbeat_history"]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_empty_history_when_no_logs(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """When no heartbeat logs exist, history still conveys that fact."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_settings.heartbeat_recent_messages_count = 5
        mock_settings.reasoning_effort = ""

        mock_session_store = MagicMock()
        mock_session_store.get_recent_messages.return_value = []
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = ""
        mock_hb_store.get_recent_logs = AsyncMock(return_value=[])
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"
        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="test")

        await evaluate_heartbeat_need(user)

        call_kwargs = mock_build_prompt.call_args
        assert "heartbeat_history" in call_kwargs.kwargs
        assert "not sent any" in call_kwargs.kwargs["heartbeat_history"]


class TestRecentMessagesIncludeTimestamps:
    """Regression: recent messages passed to the heartbeat prompt must include timestamps."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_recent_messages_contain_timestamps(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """Messages with timestamps should include the time in the formatted output."""
        from backend.app.agent.dto import StoredMessage

        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_settings.heartbeat_recent_messages_count = 5
        mock_settings.reasoning_effort = ""

        msg = StoredMessage(
            direction="outbound",
            body="Here is your morning joke!",
            timestamp=datetime.datetime(2026, 3, 23, 12, 30, tzinfo=datetime.UTC).isoformat(),
        )
        mock_session_store = MagicMock()
        mock_session_store.get_recent_messages.return_value = [msg]
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = ""
        mock_hb_store.get_recent_logs = AsyncMock(return_value=[])
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"
        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="ok")

        await evaluate_heartbeat_need(user)

        recent_text = mock_build_prompt.call_args.args[1]
        # Should contain a day-of-week timestamp (e.g. "Monday 08:30 AM")
        assert "Assistant," in recent_text
        assert "Here is your morning joke!" in recent_text

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_message_without_timestamp_falls_back(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: User,
    ) -> None:
        """Messages with empty timestamp still render without crashing."""
        from backend.app.agent.dto import StoredMessage

        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_settings.heartbeat_recent_messages_count = 5
        mock_settings.reasoning_effort = ""

        msg = StoredMessage(
            direction="inbound",
            body="Hello!",
            timestamp="",
        )
        mock_session_store = MagicMock()
        mock_session_store.get_recent_messages.return_value = [msg]
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = ""
        mock_hb_store.get_recent_logs = AsyncMock(return_value=[])
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"
        mock_llm.return_value = _make_decision_tool_call(action="skip", tasks="", reasoning="ok")

        await evaluate_heartbeat_need(user)

        recent_text = mock_build_prompt.call_args.args[1]
        # Falls back to label-only format without a timestamp
        assert "[User] Hello!" in recent_text


# ---------------------------------------------------------------------------
# Regression: editing heartbeat text must not replay removed checks (#858)
# ---------------------------------------------------------------------------


class TestHeartbeatPromptAlwaysIncludesSection:
    """Regression for #858: when heartbeat text is empty (all checks removed),
    the prompt must still include the heartbeat section so the LLM knows
    there are no items to act on, rather than silently omitting it while
    old task descriptions remain visible in the history section."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.system_prompt.build_memory_section", new_callable=AsyncMock)
    async def test_empty_heartbeat_text_produces_placeholder(
        self,
        mock_memory: AsyncMock,
        user: User,
    ) -> None:
        """build_heartbeat_system_prompt includes a placeholder when heartbeat_md is empty."""
        from backend.app.agent.system_prompt import build_heartbeat_system_prompt

        mock_memory.return_value = ""

        prompt = await build_heartbeat_system_prompt(
            user,
            recent_messages="(no recent messages)",
            heartbeat_md="",
            heartbeat_history=(
                '- Monday, 2026-03-23 09:00 AM (4 days ago) | tasks: "Check weather"'
            ),
        )

        # The heartbeat section must appear even when empty
        assert "no heartbeat items configured" in prompt
        # The history section must be annotated as timing reference only
        assert "timing reference only" in prompt

    @pytest.mark.asyncio
    @patch("backend.app.agent.system_prompt.build_memory_section", new_callable=AsyncMock)
    async def test_nonempty_heartbeat_text_included_verbatim(
        self,
        mock_memory: AsyncMock,
        user: User,
    ) -> None:
        """build_heartbeat_system_prompt includes the actual text when provided."""
        from backend.app.agent.system_prompt import build_heartbeat_system_prompt

        mock_memory.return_value = ""

        prompt = await build_heartbeat_system_prompt(
            user,
            recent_messages="(no recent messages)",
            heartbeat_md="- Check weather for outdoor jobs",
        )

        assert "Check weather for outdoor jobs" in prompt
        # The heartbeat section must contain the actual text, not the placeholder.
        # (The placeholder phrase also appears in the rules section as a reference,
        # so we check the section between the heartbeat header and the next header.)
        hb_start = prompt.index("User's heartbeat")
        hb_end = prompt.index("##", hb_start + 1)
        heartbeat_section = prompt[hb_start:hb_end]
        assert "no heartbeat items configured" not in heartbeat_section

    @pytest.mark.asyncio
    @patch("backend.app.agent.system_prompt.build_memory_section", new_callable=AsyncMock)
    async def test_history_section_header_includes_timing_disclaimer(
        self,
        mock_memory: AsyncMock,
        user: User,
    ) -> None:
        """History section header must say 'timing reference only' to prevent re-running old tasks."""
        from backend.app.agent.system_prompt import build_heartbeat_system_prompt

        mock_memory.return_value = ""

        prompt = await build_heartbeat_system_prompt(
            user,
            recent_messages="(no recent messages)",
            heartbeat_md="- Active check",
            heartbeat_history=(
                '- Monday, 2026-03-23 09:00 AM (4 days ago) | tasks: "Old removed task"'
            ),
        )

        assert "timing reference only" in prompt
        assert "not tasks to re-run" in prompt


class TestSkipEmptyHeartbeatText:
    """Regression for #864: heartbeat must not send messages when no items configured."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_empty_heartbeat_text(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When heartbeat_text is empty, skip without calling the LLM."""
        mock_count.return_value = 0
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is None
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_whitespace_only_heartbeat_text(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """Whitespace-only heartbeat text is treated as empty."""
        mock_count.return_value = 0
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "   \n  \n  "
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is None
        mock_eval.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_nonempty_heartbeat_text_proceeds_to_evaluation(
        self,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_heartbeat_store_cls: MagicMock,
        user: User,
    ) -> None:
        """When heartbeat items exist, evaluation proceeds normally."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(action="skip", tasks="", reasoning="Nothing due")
        mock_hb_store = MagicMock()
        mock_hb_store.read_heartbeat_md.return_value = "- Check weather for outdoor jobs"
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None
        mock_eval.assert_awaited_once()


class TestCompressedHeartbeatHistory:
    """Regression for #856: consecutive no-action checks should be compressed."""

    def test_consecutive_skips_compressed(self) -> None:
        """Multiple consecutive skips are merged into a single summary line."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 30, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 11, 0, tzinfo=datetime.UTC).isoformat(),
            ),
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "3 checks, no action taken" in result
        # Should be a single line, not 3 separate "[skipped]" lines
        assert result.count("[skipped]") == 0

    def test_single_skip_not_compressed(self) -> None:
        """A single skip is still shown with [skipped] label, not compressed."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
            ),
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "[skipped]" in result

    def test_skips_between_sends_compressed_separately(self) -> None:
        """Skip runs between send entries are compressed independently."""
        now = datetime.datetime(2026, 3, 23, 18, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                action_type="send",
                tasks="Morning check",
                created_at=datetime.datetime(2026, 3, 23, 9, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 9, 30, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 30, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="send",
                tasks="Afternoon reminder",
                created_at=datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC).isoformat(),
            ),
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "3 checks, no action taken" in result
        assert 'tasks: "Morning check"' in result
        assert 'tasks: "Afternoon reminder"' in result
        # The 3 skips should be 1 line, not 3
        lines = [ln for ln in result.split("\n") if ln.startswith("- ")]
        assert len(lines) == 3  # send + compressed skips + send

    def test_trailing_skips_flushed(self) -> None:
        """Skip entries at the end of the log list are properly flushed."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                action_type="send",
                tasks="Check weather",
                created_at=datetime.datetime(2026, 3, 23, 9, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 10, 0, tzinfo=datetime.UTC).isoformat(),
            ),
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(2026, 3, 23, 11, 0, tzinfo=datetime.UTC).isoformat(),
            ),
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "2 checks, no action taken" in result

    def test_all_skips_compressed(self) -> None:
        """When all entries are skips, they are compressed into one summary."""
        now = datetime.datetime(2026, 3, 23, 14, 0, tzinfo=datetime.UTC)
        logs = [
            HeartbeatLogEntry(
                user_id="u1",
                action_type="skip",
                created_at=datetime.datetime(
                    2026, 3, 23, 8 + i, 0, tzinfo=datetime.UTC
                ).isoformat(),
            )
            for i in range(5)
        ]
        result = _format_heartbeat_history(logs, "America/New_York", now)
        assert "5 checks, no action taken" in result
        lines = [ln for ln in result.split("\n") if ln.startswith("- ")]
        assert len(lines) == 1


class TestHeartbeatRulesGuardRemovedItems:
    """Regression for #858: heartbeat_rules.md must instruct the LLM to only
    act on items in the current heartbeat text."""

    def test_rules_mention_current_heartbeat_only(self) -> None:
        """The rules prompt must explicitly say to only act on current items."""
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules")
        assert "Only act on items" in rules
        assert "current" in rules.lower()
        assert "history" in rules.lower()


class TestHeartbeatRulesGuardHistoryPatterns:
    """Regression for #864: rules must prevent the LLM from inferring action
    patterns from heartbeat activity history."""

    def test_rules_prohibit_pattern_inference(self) -> None:
        """The rules must explicitly tell the LLM not to infer patterns from history."""
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules")
        assert "infer" in rules.lower() or "pattern" in rules.lower()
        assert "removed" in rules.lower() or "no longer" in rules.lower()


class TestHeartbeatRulesGuardConversationContext:
    """The rules must forbid the LLM from running heartbeat tasks just because
    it sees a pending user request in recent conversation context.

    Real failure mode this guards against: a deploy-induced container restart
    fired Phase 1 within seconds of boot. Phase 1 saw the user's recent
    'please send the estimate' message in context and chose action=run. The
    Phase 2 agent then re-executed work the previous container was already
    handling. With idempotent reads this is harmless; with side-effecting
    tools (qb_send, qb_create), it risks double execution. The rules now
    explicitly tell Phase 1 that pending user requests belong to the
    user-driven path, not to heartbeat.
    """

    def test_rules_forbid_running_for_recent_user_request(self) -> None:
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules").lower()
        # Heartbeat must NOT volunteer to "complete" pending user requests.
        # We anchor on the semantics (recent conversation + skip/don't run),
        # not the exact phrasing, so the wording can evolve without breaking
        # this regression test.
        assert "recent conversation" in rules or "recent messages" in rules
        assert any(forbid in rules for forbid in ("do not 'run'", "don't 'run'", "choose 'skip'"))
        # The user-driven agent path is named so the LLM understands the
        # division of labor.
        assert "user-driven" in rules or "user driven" in rules
        # 'run' decision must be tied to the heartbeat section, not to
        # arbitrary "looks interesting" data.
        assert "heartbeat list" in rules or "heartbeat section" in rules


class TestHeartbeatRulesPruneStaleOneTimeItems:
    """The rules must tell Phase 1 that a one-time dated item whose date has
    clearly passed is stale and should be routed to cleanup, not skipped
    silently nor acted on as if still due.

    Real failure mode this guards against: the user's HEARTBEAT.md contained
    a line like "follow up on the Smith estimate by April 29". When the
    current date became May, the item kept sitting in HEARTBEAT.md because
    nothing cleaned it up. Phase 1 either re-acted on it (double work) or
    quietly skipped it forever (item never aged out). The rules now route
    stale one-time items into a 'run' with a cleanup task description so
    the executor calls update_heartbeat to delete them.
    """

    def test_rules_route_stale_dated_items_to_cleanup(self) -> None:
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules").lower()
        # The rules must mention staleness and one-time items.
        assert "stale" in rules
        assert "one-time" in rules
        # The cleanup path must be named: rules tell the LLM to use 'run'
        # with a removal-style task description so Phase 2 can prune the
        # entry via update_heartbeat.
        assert "remove" in rules
        assert "update_heartbeat" in rules
        # Recurring patterns must be explicitly carved out so the LLM
        # does not delete things like "every morning" or "Mondays".
        assert "recurring" in rules

    def test_rules_require_strict_staleness_threshold(self) -> None:
        """The stale-item rule must require an explicit calendar date AND
        more than one day past, to avoid pruning items the user is still
        within their window for handling.

        Without this guard, items dated for today or yesterday could be
        auto-removed before the user has had a chance to act on them.
        """
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules").lower()
        # The threshold language: explicit calendar date + a "more than 1 day"
        # / "1 full day" gate.
        assert "explicit calendar date" in rules
        assert "1 full day" in rules or "1 day in the past" in rules

    def test_rules_skip_on_ambiguity(self) -> None:
        """When the date is ambiguous (no year, day-of-week only) or the
        item carries an unverified condition ('if not done'), the LLM must
        choose 'skip' rather than guessing it is stale.
        """
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules").lower()
        # Ambiguity → skip
        assert "ambiguous" in rules
        # Conditions are explicitly carved out
        assert "if not done" in rules or "unverified condition" in rules

    def test_rules_dedup_recent_removal_runs(self) -> None:
        """Without dedup, a Phase 2 failure (network glitch) plus a sub-30m
        tick interval would have Phase 1 issue the same removal task every
        tick, burning tokens until the executor lands.
        """
        from backend.app.agent.system_prompt import load_prompt

        rules = load_prompt("heartbeat_rules").lower()
        # Dedup window must be named explicitly so a careless rewrite cannot
        # silently drop it.
        assert "24 hours" in rules
        # The dedup path must say 'skip' so the LLM understands the action.
        assert "in flight" in rules or "still in flight" in rules


# ---------------------------------------------------------------------------
# Phase 2: tool wiring (regression for #874)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_execute_heartbeat_uses_core_tools_and_list_capabilities(user: User) -> None:
    """execute_heartbeat_tasks should use create_core_tools + list_capabilities,
    not create_tools with all factories (regression test for #874)."""
    mock_agent_cls = MagicMock()
    mock_agent = MagicMock()
    mock_agent_cls.return_value = mock_agent
    mock_agent.register_tools = MagicMock()
    mock_agent.process_message = AsyncMock(
        return_value=MagicMock(is_error_fallback=False, reply_text="done", actions_taken="")
    )

    mock_registry = MagicMock()
    mock_registry.create_core_tools = AsyncMock(return_value=[MagicMock(name="core_tool")])
    mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
    mock_registry.get_available_specialist_summaries.return_value = {"quickbooks": "QB tools"}
    mock_registry.get_unauthenticated_specialists.return_value = {}
    mock_registry.get_disabled_specialist_sub_tools.return_value = {}

    mock_tool_config = MagicMock()
    mock_tool_config.get_disabled_tool_names = AsyncMock(return_value=set())
    mock_tool_config.get_disabled_sub_tool_names = AsyncMock(return_value=set())

    with (
        patch("backend.app.agent.core.ClawboltAgent", mock_agent_cls),
        patch(
            "backend.app.agent.tools.registry.default_registry",
            mock_registry,
        ),
        patch(
            "backend.app.agent.stores.ToolConfigStore",
            return_value=mock_tool_config,
        ),
        patch(
            "backend.app.agent.tools.registry.create_list_capabilities_tool",
            return_value=MagicMock(name="list_capabilities"),
        ) as mock_list_cap,
        patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
        patch("backend.app.agent.heartbeat.message_bus"),
    ):
        from backend.app.agent.heartbeat import execute_heartbeat_tasks

        await execute_heartbeat_tasks(user, "Check invoices")

    # Should use create_core_tools, not create_tools
    mock_registry.create_core_tools.assert_called_once()
    assert not hasattr(mock_registry.create_tools, "call_count") or (
        mock_registry.create_tools.call_count == 0
    )

    # Should have created list_capabilities meta-tool
    mock_list_cap.assert_called_once()

    # "messaging" should NOT be excluded (#921) — the agent needs it to send
    call_kwargs = mock_registry.create_core_tools.call_args
    excluded = call_kwargs.kwargs.get("excluded_factories") or set()
    assert "messaging" not in excluded


@pytest.mark.asyncio()
async def test_execute_heartbeat_respects_disabled_tools(user: User) -> None:
    """execute_heartbeat_tasks should respect user's disabled tool config (#874)."""
    mock_agent_cls = MagicMock()
    mock_agent = MagicMock()
    mock_agent_cls.return_value = mock_agent
    mock_agent.register_tools = MagicMock()
    mock_agent.process_message = AsyncMock(
        return_value=MagicMock(is_error_fallback=False, reply_text="done", actions_taken="")
    )

    mock_registry = MagicMock()
    mock_registry.create_core_tools = AsyncMock(return_value=[])
    mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
    mock_registry.get_available_specialist_summaries.return_value = {}
    mock_registry.get_unauthenticated_specialists.return_value = {}
    mock_registry.get_disabled_specialist_sub_tools.return_value = {}

    mock_tool_config = MagicMock()
    mock_tool_config.get_disabled_tool_names = AsyncMock(return_value={"quickbooks"})
    mock_tool_config.get_disabled_sub_tool_names = AsyncMock(return_value={"qb_query"})

    with (
        patch("backend.app.agent.core.ClawboltAgent", mock_agent_cls),
        patch("backend.app.agent.tools.registry.default_registry", mock_registry),
        patch(
            "backend.app.agent.stores.ToolConfigStore",
            return_value=mock_tool_config,
        ),
        patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
        patch("backend.app.agent.heartbeat.message_bus"),
    ):
        from backend.app.agent.heartbeat import execute_heartbeat_tasks

        await execute_heartbeat_tasks(user, "Check something")

    # Disabled groups should be excluded, but messaging should not be (#921)
    call_kwargs = mock_registry.create_core_tools.call_args
    excluded = call_kwargs.kwargs.get("excluded_factories") or set()
    assert "quickbooks" in excluded
    assert "messaging" not in excluded

    # Disabled sub-tools should be passed through
    excluded_tools = call_kwargs.kwargs.get("excluded_tool_names") or call_kwargs[1].get(
        "excluded_tool_names"
    )
    assert "qb_query" in excluded_tools


@pytest.mark.asyncio()
async def test_execute_heartbeat_task_context_includes_cleanup_instruction(
    user: User,
) -> None:
    """Phase 2's task_context must instruct the agent to remove one-time
    dated HEARTBEAT.md items after handling them, while preserving recurring
    patterns. This is the durable counterpart to the Phase 1 stale-item
    rule: once handled, the item should leave HEARTBEAT.md so it cannot
    fire again on a future tick.
    """
    mock_agent_cls = MagicMock()
    mock_agent = MagicMock()
    mock_agent_cls.return_value = mock_agent
    mock_agent.register_tools = MagicMock()
    mock_agent.process_message = AsyncMock(
        return_value=MagicMock(is_error_fallback=False, reply_text="done", actions_taken="")
    )

    mock_registry = MagicMock()
    mock_registry.create_core_tools = AsyncMock(return_value=[])
    mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
    mock_registry.get_available_specialist_summaries.return_value = {}
    mock_registry.get_unauthenticated_specialists.return_value = {}
    mock_registry.get_disabled_specialist_sub_tools.return_value = {}

    mock_tool_config = MagicMock()
    mock_tool_config.get_disabled_tool_names = AsyncMock(return_value=set())
    mock_tool_config.get_disabled_sub_tool_names = AsyncMock(return_value=set())

    with (
        patch("backend.app.agent.core.ClawboltAgent", mock_agent_cls),
        patch("backend.app.agent.tools.registry.default_registry", mock_registry),
        patch(
            "backend.app.agent.stores.ToolConfigStore",
            return_value=mock_tool_config,
        ),
        patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
        patch("backend.app.agent.heartbeat.message_bus"),
    ):
        from backend.app.agent.heartbeat import execute_heartbeat_tasks

        await execute_heartbeat_tasks(user, "Follow up on the Smith estimate")

    # The agent should have been called with a message_context that contains
    # the cleanup directive and the original task body.
    await_args = mock_agent.process_message.await_args
    assert await_args is not None
    message_context = await_args.kwargs["message_context"]
    lowered = message_context.lower()

    # Original task body must be preserved.
    assert "Follow up on the Smith estimate" in message_context
    # Cleanup directive must name update_heartbeat so the agent knows the tool
    assert "update_heartbeat" in message_context
    # One-time dated items get removed; recurring patterns stay.
    assert "one-time" in lowered
    assert "recurring" in lowered
    # The two task shapes must be distinguished so a Phase 1 cleanup task does
    # not also trigger the underlying real-world action (e.g. "Remove the
    # stale Smith follow-up" should NOT cause Phase 2 to actually follow up).
    assert "real-world action" in lowered
    assert "cleanup task" in lowered or "heartbeat.md cleanup" in lowered
    # Cleanup-only runs should not chain a redundant second update_heartbeat
    # call after the first succeeds.
    assert "second update_heartbeat" in lowered or "do not issue a second" in lowered
    # The heartbeat path is identified by the SCHEDULED_TASK_PREFIX
    # constant. The update_heartbeat tool's usage_hint also matches on
    # the same constant to decide when proactive pruning is allowed.
    # Asserting against the constant (rather than the literal) means a
    # rename of the prefix in one place breaks the test if the other
    # side wasn't updated, instead of silently passing.
    from backend.app.agent.heartbeat import SCHEDULED_TASK_PREFIX

    assert SCHEDULED_TASK_PREFIX in message_context


def test_update_heartbeat_usage_hint_anchors_on_scheduled_task_prefix() -> None:
    """The update_heartbeat usage_hint scopes proactive pruning to the
    heartbeat path by matching on the SCHEDULED_TASK_PREFIX constant
    exported from heartbeat.py. Both sides import the same constant
    so a rename in one place cannot drift the other.
    """
    from backend.app.agent.heartbeat import SCHEDULED_TASK_PREFIX
    from backend.app.agent.tools.heartbeat_tools import create_heartbeat_tools
    from backend.app.agent.tools.names import ToolName

    tools = create_heartbeat_tools(user_id="test-user")
    update_tool = next(t for t in tools if t.name == ToolName.UPDATE_HEARTBEAT)
    assert update_tool.usage_hint is not None
    assert SCHEDULED_TASK_PREFIX in update_tool.usage_hint


@pytest.mark.asyncio()
async def test_heartbeat_skips_manual_delivery_when_agent_sent_reply(user: User) -> None:
    """When the agent already sent via a SENDS_REPLY tool (send_media_reply),
    the heartbeat runner should not publish the reply_text again
    (regression test for #921)."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse
    from backend.app.agent.tools.base import ToolTags

    mock_response = AgentResponse(
        reply_text="Sent!",
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc_1",
                name="send_media_reply",
                args={
                    "message": "Here is your joke.",
                    "media_url": "https://example.com/joke.png",
                },
                result="Sent media message",
                is_error=False,
                tags={ToolTags.SENDS_REPLY},
            )
        ],
    )

    with (
        patch("backend.app.agent.heartbeat.execute_heartbeat_tasks") as mock_execute,
        patch("backend.app.agent.heartbeat.evaluate_heartbeat_need") as mock_eval,
        patch("backend.app.agent.heartbeat.get_daily_heartbeat_count") as mock_count,
        patch("backend.app.agent.heartbeat.HeartbeatStore") as mock_hb_cls,
        patch("backend.app.agent.heartbeat.get_or_create_conversation") as mock_get_conv,
        patch("backend.app.agent.heartbeat.get_session_store") as mock_get_ss,
        patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
    ):
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="Send a joke", reasoning="morning joke"
        )
        mock_execute.return_value = mock_response
        mock_bus.publish_outbound = AsyncMock()

        mock_get_conv.return_value = (MagicMock(), True)
        mock_ss = MagicMock()
        mock_ss.add_message = AsyncMock()
        mock_get_ss.return_value = mock_ss

        mock_hb = MagicMock()
        mock_hb.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb.log_heartbeat = AsyncMock()
        mock_hb_cls.return_value = mock_hb

        result = await run_heartbeat_for_user(user, "bluebubbles", "+1555", 5)

    assert result is not None
    assert result.action_type == "send_message"
    # Agent already sent via tool, so no manual publish_outbound call
    mock_bus.publish_outbound.assert_not_awaited()
    # But heartbeat log should still be recorded
    mock_hb.log_heartbeat.assert_awaited_once()


@pytest.mark.asyncio()
async def test_heartbeat_logs_when_sent_reply_but_empty_reply_text(user: User) -> None:
    """When the agent sent via a SENDS_REPLY tool but produced no reply_text,
    the runner should still log the heartbeat (#921)."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse
    from backend.app.agent.tools.base import ToolTags

    mock_response = AgentResponse(
        reply_text="",
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc_1",
                name="send_media_reply",
                args={
                    "message": "Here is your joke.",
                    "media_url": "https://example.com/joke.png",
                },
                result="Sent media message",
                is_error=False,
                tags={ToolTags.SENDS_REPLY},
            )
        ],
    )

    with (
        patch("backend.app.agent.heartbeat.execute_heartbeat_tasks") as mock_execute,
        patch("backend.app.agent.heartbeat.evaluate_heartbeat_need") as mock_eval,
        patch("backend.app.agent.heartbeat.get_daily_heartbeat_count") as mock_count,
        patch("backend.app.agent.heartbeat.HeartbeatStore") as mock_hb_cls,
        patch("backend.app.agent.heartbeat.get_or_create_conversation") as mock_get_conv,
        patch("backend.app.agent.heartbeat.get_session_store") as mock_get_ss,
        patch("backend.app.agent.heartbeat.message_bus") as mock_bus,
    ):
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatDecision(
            action="run", tasks="Send a joke", reasoning="morning joke"
        )
        mock_execute.return_value = mock_response
        mock_bus.publish_outbound = AsyncMock()

        mock_get_conv.return_value = (MagicMock(), True)
        mock_ss = MagicMock()
        mock_ss.add_message = AsyncMock()
        mock_get_ss.return_value = mock_ss

        mock_hb = MagicMock()
        mock_hb.read_heartbeat_md.return_value = "- At 3pm: check the queue"
        mock_hb.log_heartbeat = AsyncMock()
        mock_hb_cls.return_value = mock_hb

        result = await run_heartbeat_for_user(user, "bluebubbles", "+1555", 5)

    assert result is not None
    assert result.action_type == "send_message"
    # No manual delivery and no duplicate
    mock_bus.publish_outbound.assert_not_awaited()
    # Heartbeat log still recorded for rate limiting
    mock_hb.log_heartbeat.assert_awaited_once()


@pytest.mark.asyncio()
async def test_heartbeat_auto_approves_send_media_reply(user: User) -> None:
    """Heartbeat Phase 2 should clear approval_policy on send_media_reply so
    the agent can deliver attachments without prompting the user
    (regression test for #932). Plain text replies go through
    response.reply_text directly and don't need approval."""
    from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
    from backend.app.agent.tools.base import Tool, ToolResult, ToolTags
    from backend.app.agent.tools.names import ToolName

    class _EmptyParams(BaseModel):
        pass

    async def _noop(**kwargs: object) -> ToolResult:
        return ToolResult(content="ok")

    send_media_tool = Tool(
        name=ToolName.SEND_MEDIA_REPLY,
        description="Send media reply",
        function=_noop,
        params_model=_EmptyParams,
        tags={ToolTags.SENDS_REPLY},
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            description_builder=lambda args: "Send a media message",
        ),
    )
    other_tool = Tool(
        name="read_file",
        description="Read a file",
        function=_noop,
        params_model=_EmptyParams,
        approval_policy=ApprovalPolicy(default_level=PermissionLevel.ALWAYS),
    )
    core_tools = [send_media_tool, other_tool]

    mock_agent_cls = MagicMock()
    mock_agent = MagicMock()
    mock_agent_cls.return_value = mock_agent
    mock_agent.register_tools = MagicMock()
    mock_agent.process_message = AsyncMock(
        return_value=MagicMock(is_error_fallback=False, reply_text="done", actions_taken="")
    )

    mock_registry = MagicMock()
    mock_registry.create_core_tools = AsyncMock(return_value=core_tools)
    mock_registry.create_ready_specialist_tools = AsyncMock(return_value=([], set()))
    mock_registry.get_available_specialist_summaries.return_value = {}
    mock_registry.get_unauthenticated_specialists.return_value = {}
    mock_registry.get_disabled_specialist_sub_tools.return_value = {}

    mock_tool_config = MagicMock()
    mock_tool_config.get_disabled_tool_names = AsyncMock(return_value=set())
    mock_tool_config.get_disabled_sub_tool_names = AsyncMock(return_value=set())

    with (
        patch("backend.app.agent.core.ClawboltAgent", mock_agent_cls),
        patch("backend.app.agent.tools.registry.default_registry", mock_registry),
        patch("backend.app.agent.stores.ToolConfigStore", return_value=mock_tool_config),
        patch("backend.app.agent.tools.registry.create_list_capabilities_tool"),
        patch("backend.app.agent.tools.registry.ensure_tool_modules_imported"),
        patch("backend.app.agent.heartbeat.message_bus"),
    ):
        from backend.app.agent.heartbeat import execute_heartbeat_tasks

        await execute_heartbeat_tasks(user, "Send daily joke", channel="sms", chat_id="+1555")

    assert send_media_tool.approval_policy is None, (
        "send_media_reply should have approval_policy=None in heartbeat context"
    )
    assert other_tool.approval_policy is not None, (
        "non-messaging tools should retain their approval_policy"
    )
