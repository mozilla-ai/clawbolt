"""Tests for the proactive heartbeat engine."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage

from backend.app.agent.file_store import (
    HeartbeatLogEntry,
    UserData,
    get_user_store,
)
from backend.app.agent.heartbeat import (
    _NON_PUSHABLE_CHANNELS,
    COMPOSE_MESSAGE_TOOL,
    ComposeMessageParams,
    HeartbeatAction,
    HeartbeatScheduler,
    _parse_tool_call_response,
    _pick_heartbeat_channel,
    evaluate_heartbeat_need,
    get_daily_heartbeat_count,
    is_within_business_hours,
    parse_frequency_to_minutes,
    run_heartbeat_for_user,
)
from backend.app.agent.system_prompt import to_local_time
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user() -> UserData:
    return UserData(
        id=1,
        user_id="hb-user-001",
        phone="+15559990000",
        onboarding_complete=True,
    )


@pytest.fixture()
def user_with_timezone() -> UserData:
    return UserData(
        id=3,
        user_id="hb-user-003",
        phone="+15559990002",
        timezone="America/Los_Angeles",
        onboarding_complete=True,
    )


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


# ---------------------------------------------------------------------------
# is_within_business_hours
# ---------------------------------------------------------------------------


class TestIsWithinBusinessHours:
    @patch("backend.app.agent.heartbeat.settings")
    def test_outside_quiet_hours(self, mock_settings: MagicMock, user: UserData) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 10 AM -- outside quiet hours, should be True
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_inside_quiet_hours_evening(self, mock_settings: MagicMock, user: UserData) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 22:00 -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 22, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_inside_quiet_hours_early_morning(
        self, mock_settings: MagicMock, user: UserData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 3 AM -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 3, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user, now) is False


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


class TestIsWithinBusinessHoursTimezone:
    """Tests for timezone-correct quiet hour checks."""

    @patch("backend.app.agent.heartbeat.settings")
    def test_timezone_converts_before_quiet_check(
        self, mock_settings: MagicMock, user_with_timezone: UserData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 2 PM UTC -> 7 AM Pacific (PDT). Outside quiet hours.
        now = datetime.datetime(2025, 6, 15, 14, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user_with_timezone, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_utc_morning_is_night_in_pacific(
        self, mock_settings: MagicMock, user_with_timezone: UserData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 5 AM UTC -> 10 PM Pacific (PDT, previous day). Inside quiet hours.
        now = datetime.datetime(2025, 6, 15, 5, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user_with_timezone, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_no_timezone_uses_utc(self, mock_settings: MagicMock, user: UserData) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # User without timezone uses UTC directly.
        # 10 AM UTC, outside quiet hours.
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(user, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_invalid_timezone_falls_back_to_utc(self, mock_settings: MagicMock) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        c = UserData(
            id=99,
            user_id="hb-user-bad-tz",
            timezone="Invalid/Timezone",
            onboarding_complete=True,
        )
        # 10 AM UTC -> falls back to UTC -> outside quiet hours
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(c, now) is True


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
        resp = MessageResponse(
            id="msg_mock",
            content=[
                MessageContentBlock(
                    type="tool_use",
                    id="call_bad",
                    name="compose_message",
                    input=None,
                ),
            ],
            model="mock-model",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert "Malformed tool arguments" in action.reasoning

    def test_none_arguments_does_not_crash(self) -> None:
        """None tool input should not raise TypeError."""
        resp = MessageResponse(
            id="msg_mock",
            content=[
                MessageContentBlock(
                    type="tool_use",
                    id="call_none_args",
                    name="compose_message",
                    input=None,
                ),
            ],
            model="mock-model",
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
                MessageContentBlock(
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
                MessageContentBlock(
                    type="tool_use",
                    id="call_no_msg",
                    name="compose_message",
                    input={"action": "no_action", "reasoning": "nothing", "priority": 2},
                ),
            ],
            model="mock-model",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        assert action.message == ""
        assert action.priority == 2

    def test_nameless_tool_use_falls_back(self) -> None:
        """tool_use block with no name should fall back to no_action."""
        resp = MessageResponse(
            id="msg_mock",
            content=[
                MessageContentBlock(
                    type="tool_use",
                    id="call_nofunc",
                    name=None,
                    input={"action": "send_message"},
                ),
            ],
            model="mock-model",
            stop_reason="tool_use",
            usage=MessageUsage(input_tokens=0, output_tokens=0),
        )
        action = _parse_tool_call_response(resp)
        assert action.action_type == "no_action"
        # Parsed tool call has empty name, so heartbeat reports "unexpected tool"
        assert "unexpected tool" in action.reasoning


# ---------------------------------------------------------------------------
# evaluate_heartbeat_need
# ---------------------------------------------------------------------------


class TestEvaluateHeartbeatNeed:
    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_no(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: UserData,
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
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action",
            message="",
            reasoning="Nothing actionable",
            priority=1,
        )
        action = await evaluate_heartbeat_need(user)
        assert action.action_type == "no_action"
        assert action.message == ""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_send(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: UserData,
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
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="send_message",
            message="Hey Mike, you have a draft estimate sitting for 2 days.",
            reasoning="Stale draft estimate",
            priority=4,
        )
        action = await evaluate_heartbeat_need(user)
        assert action.action_type == "send_message"
        assert "draft estimate" in action.message

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
        user: UserData,
    ) -> None:
        """When heartbeat_model is configured, it should be used instead of llm_model."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = "gpt-4o-mini"
        mock_settings.heartbeat_provider = "openai"
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_settings.heartbeat_recent_messages_count = 5

        mock_session_store = MagicMock()
        mock_session_store.get_recent_messages.return_value = []
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="", priority=1
        )
        await evaluate_heartbeat_need(user)

        # Verify the cheap model was used
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
        user: UserData,
    ) -> None:
        """Regression test: acompletion must receive api_base, not api_key."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = "http://localhost:1234/v1"
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

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
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
    async def test_text_response_falls_back_to_no_action(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: UserData,
    ) -> None:
        """If LLM returns text instead of tool call, default to no_action."""
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
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"

        mock_llm.return_value = make_text_response("I'm not sure what to do {broken json")
        action = await evaluate_heartbeat_need(user)
        assert action.action_type == "no_action"
        assert action.priority == 0

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_tools_to_acompletion(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: UserData,
    ) -> None:
        """acompletion should receive tools=[COMPOSE_MESSAGE_TOOL]."""
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
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "system prompt"

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
        await evaluate_heartbeat_need(user)
        _, kwargs = mock_llm.call_args
        assert "tools" in kwargs
        assert kwargs["tools"] == [COMPOSE_MESSAGE_TOOL]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.log_llm_usage")
    @patch("backend.app.agent.heartbeat.build_heartbeat_system_prompt", new_callable=AsyncMock)
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_prompt_does_not_ask_for_raw_json(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        mock_build_prompt: AsyncMock,
        mock_log_usage: MagicMock,
        user: UserData,
    ) -> None:
        """System prompt should not contain 'Respond with ONLY a JSON object'."""
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
        mock_hb_store.read_checklist_md.return_value = ""
        mock_heartbeat_store_cls.return_value = mock_hb_store

        mock_build_prompt.return_value = "Use compose_message tool to decide"

        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
        await evaluate_heartbeat_need(user)
        call_args = mock_llm.call_args
        system_content = call_args.kwargs["system"]
        assert "Respond with ONLY a JSON object" not in system_content
        assert "compose_message" in system_content


# ---------------------------------------------------------------------------
# run_heartbeat_for_user
# ---------------------------------------------------------------------------


class TestRunHeartbeatForUser:
    @pytest.mark.asyncio
    async def test_skip_not_onboarded(self) -> None:
        c = UserData(id=10, user_id="hb-new", phone="+15550000000", onboarding_complete=False)
        result = await run_heartbeat_for_user(c, "telegram", c.phone, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=False)
    async def test_skip_outside_hours(
        self,
        _mock_hours: MagicMock,
        user: UserData,
    ) -> None:
        result = await run_heartbeat_for_user(user, "telegram", user.phone, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_rate_limited(
        self,
        mock_count: AsyncMock,
        _mock_hours: MagicMock,
        user: UserData,
    ) -> None:
        mock_count.return_value = 5
        result = await run_heartbeat_for_user(user, "telegram", user.phone, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_no_action_from_llm(
        self,
        _mock_hours: MagicMock,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        user: UserData,
    ) -> None:
        """When LLM returns no_action, no message is sent."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="Nothing actionable right now",
            priority=1,
        )
        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        assert result is not None
        assert result.action_type == "no_action"
        mock_eval.assert_awaited_once_with(user, channel="telegram", chat_id="+15559990000")

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.HeartbeatStore")
    @patch("backend.app.agent.heartbeat.get_session_store")
    @patch("backend.app.agent.heartbeat.get_or_create_conversation")
    @patch("backend.app.bus.OutboundMessage")
    @patch("backend.app.bus.message_bus")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_send_message_and_record(
        self,
        _mock_hours: MagicMock,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        mock_get_conv: AsyncMock,
        mock_get_session_store: MagicMock,
        mock_heartbeat_store_cls: MagicMock,
        user: UserData,
    ) -> None:
        """When LLM says send, message is delivered via the bus."""
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Reminder: draft estimate pending!",
            reasoning="Stale draft",
            priority=4,
        )
        mock_bus.publish_outbound = AsyncMock()

        mock_session = MagicMock()
        mock_get_conv.return_value = (mock_session, True)

        mock_session_store = MagicMock()
        mock_session_store.add_message = AsyncMock()
        mock_get_session_store.return_value = mock_session_store

        mock_hb_store = MagicMock()
        mock_hb_store.log_heartbeat = AsyncMock()
        mock_heartbeat_store_cls.return_value = mock_hb_store

        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)

        assert result is not None
        assert result.action_type == "send_message"
        # Verify outbound message was published to the bus
        mock_bus.publish_outbound.assert_awaited_once()
        mock_outbound_msg.assert_called_once_with(
            channel="telegram",
            chat_id="+15559990000",
            content="Reminder: draft estimate pending!",
        )
        # LLM was called with new keyword args (no flags)
        mock_eval.assert_awaited_once_with(user, channel="telegram", chat_id="+15559990000")

    @pytest.mark.asyncio
    @patch("backend.app.bus.OutboundMessage")
    @patch("backend.app.bus.message_bus")
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_sms_failure_graceful(
        self,
        _mock_hours: MagicMock,
        mock_count: AsyncMock,
        mock_eval: AsyncMock,
        mock_bus: MagicMock,
        mock_outbound_msg: MagicMock,
        user: UserData,
    ) -> None:
        mock_count.return_value = 0
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Reminder!",
            reasoning="test",
            priority=3,
        )
        mock_bus.publish_outbound = AsyncMock(side_effect=Exception("Bus down"))
        result = await run_heartbeat_for_user(user, "telegram", "+15559990000", 5)
        # Should still return the action, just not record a message
        assert result is not None
        assert result.action_type == "send_message"


# ---------------------------------------------------------------------------
# get_daily_heartbeat_count (persistent rate limiting)
# ---------------------------------------------------------------------------


class TestGetDailyHeartbeatCount:
    @pytest.mark.asyncio
    async def test_zero_when_no_logs(self, user: UserData) -> None:
        assert await get_daily_heartbeat_count(user.id) == 0

    @pytest.mark.asyncio
    async def test_counts_today_only(self, user: UserData) -> None:
        """Logs from yesterday should not count toward today's limit."""
        from backend.app.agent.file_store import HeartbeatStore, _append_jsonl

        store = HeartbeatStore(user.id)
        # Add a log from today
        await store.log_heartbeat()
        # Add a log from yesterday directly to the JSONL file
        yesterday = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        entry = HeartbeatLogEntry(user_id=user.id, created_at=yesterday.isoformat())
        _append_jsonl(store._log_path, entry.model_dump())

        assert await get_daily_heartbeat_count(user.id) == 1

    @pytest.mark.asyncio
    async def test_counts_multiple_today(self, user: UserData) -> None:
        from backend.app.agent.file_store import HeartbeatStore

        store = HeartbeatStore(user.id)
        for _ in range(3):
            await store.log_heartbeat()

        assert await get_daily_heartbeat_count(user.id) == 3

    @pytest.mark.asyncio
    async def test_scoped_to_user(self, user: UserData) -> None:
        """Logs from other users should not count."""
        from backend.app.agent.file_store import HeartbeatStore

        other = UserData(
            id=60,
            user_id="hb-other",
            phone="+15551112222",
            onboarding_complete=True,
        )

        other_store = HeartbeatStore(other.id)
        await other_store.log_heartbeat()

        assert await get_daily_heartbeat_count(user.id) == 0
        assert await get_daily_heartbeat_count(other.id) == 1


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
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_queries_onboarded(
        self, mock_default_channel: MagicMock, mock_get_store: MagicMock
    ) -> None:
        """Tick should query all users via list_all and filter by onboarding_complete."""
        mock_store = AsyncMock()
        mock_store.list_all.return_value = []
        mock_get_store.return_value = mock_store

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_store.list_all.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_concurrent_processing(
        self,
        mock_settings: MagicMock,
        mock_default_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """tick() should process multiple users concurrently."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create mock users
        users = []
        for i in range(4):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            c.channel_identifier = ""
            c.phone = "+15559990000"
            users.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = users
        mock_get_store.return_value = mock_store

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # run_heartbeat_for_user called once per user
        assert mock_run.await_count == len(users)

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_error_isolation(
        self,
        mock_settings: MagicMock,
        mock_default_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """One user failure should not prevent others from being processed."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5

        users = []
        for i in range(3):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            c.channel_identifier = ""
            c.phone = "+15559990000"
            users.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = users
        mock_get_store.return_value = mock_store

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
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_semaphore_limits_concurrency(
        self,
        mock_settings: MagicMock,
        mock_default_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Semaphore should limit the number of concurrent user evaluations."""
        concurrency_limit = 2
        mock_settings.heartbeat_concurrency = concurrency_limit
        mock_settings.heartbeat_max_daily_messages = 5

        users = []
        for i in range(5):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            c.channel_identifier = ""
            c.phone = "+15559990000"
            users.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = users
        mock_get_store.return_value = mock_store

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
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_no_users(
        self, mock_default_channel: MagicMock, mock_get_store: MagicMock
    ) -> None:
        """tick() with no onboarded users should return early."""
        mock_store = AsyncMock()
        mock_store.list_all.return_value = []
        mock_get_store.return_value = mock_store

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_store.list_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# _pick_heartbeat_channel
# ---------------------------------------------------------------------------


class TestPickHeartbeatChannel:
    """Heartbeat should route to a pushable channel, never to webchat."""

    def test_webchat_in_non_pushable(self) -> None:
        """webchat must be listed as a non-pushable channel."""
        assert "webchat" in _NON_PUSHABLE_CHANNELS

    @patch("backend.app.agent.heartbeat.get_channel")
    def test_preferred_channel_is_pushable(self, mock_get_channel: MagicMock) -> None:
        """When preferred_channel is pushable, use it directly."""
        user = UserData(id=1, preferred_channel="telegram")
        mock_get_channel.return_value = MagicMock()

        result = _pick_heartbeat_channel(user)

        mock_get_channel.assert_called_once_with("telegram")
        assert result == "telegram"

    @patch("backend.app.agent.heartbeat.get_manager")
    @patch("backend.app.agent.heartbeat.get_channel")
    def test_webchat_preferred_falls_back_to_telegram(
        self, mock_get_channel: MagicMock, mock_get_manager: MagicMock
    ) -> None:
        """When preferred_channel is webchat, fall back to the first pushable channel."""
        user = UserData(id=1, preferred_channel="webchat")

        mock_manager = MagicMock()
        mock_manager.channels = {"telegram": MagicMock(), "webchat": MagicMock()}
        mock_get_manager.return_value = mock_manager

        result = _pick_heartbeat_channel(user)

        mock_get_channel.assert_not_called()
        assert result == "telegram"

    @patch("backend.app.agent.heartbeat.get_manager")
    @patch("backend.app.agent.heartbeat.get_channel")
    def test_unregistered_preferred_falls_back(
        self, mock_get_channel: MagicMock, mock_get_manager: MagicMock
    ) -> None:
        """When preferred_channel is not registered, fall back to first pushable."""
        user = UserData(id=1, preferred_channel="sms")
        mock_get_channel.side_effect = KeyError("sms not registered")

        mock_manager = MagicMock()
        mock_manager.channels = {"telegram": MagicMock()}
        mock_get_manager.return_value = mock_manager

        result = _pick_heartbeat_channel(user)

        assert result == "telegram"

    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.get_manager")
    @patch("backend.app.agent.heartbeat.get_channel")
    def test_no_pushable_channels_falls_back_to_default(
        self,
        mock_get_channel: MagicMock,
        mock_get_manager: MagicMock,
        mock_get_default: MagicMock,
    ) -> None:
        """When only non-pushable channels are registered, fall back to default."""
        user = UserData(id=1, preferred_channel="webchat")
        mock_default = MagicMock()
        mock_default.name = "webchat"
        mock_get_default.return_value = mock_default

        mock_manager = MagicMock()
        mock_manager.channels = {"webchat": MagicMock()}
        mock_get_manager.return_value = mock_manager

        result = _pick_heartbeat_channel(user)

        mock_get_default.assert_called_once()
        assert result == "webchat"

    @patch("backend.app.agent.heartbeat.get_manager")
    @patch("backend.app.agent.heartbeat.get_channel")
    def test_webchat_skipped_even_when_first_registered(
        self, mock_get_channel: MagicMock, mock_get_manager: MagicMock
    ) -> None:
        """webchat should be skipped even if it is the first registered channel."""
        user = UserData(id=1, preferred_channel="webchat")

        mock_manager = MagicMock()
        mock_manager.channels = {"webchat": MagicMock(), "telegram": MagicMock()}
        mock_get_manager.return_value = mock_manager

        result = _pick_heartbeat_channel(user)

        assert result == "telegram"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_uses_pick_heartbeat_channel(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """tick() should use _pick_heartbeat_channel instead of get_channel."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.preferred_channel = "webchat"
        user.channel_identifier = ""
        user.phone = "+15559990000"

        mock_store = AsyncMock()
        mock_store.list_all.return_value = [user]
        mock_get_store.return_value = mock_store

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_pick_channel.assert_called_once_with(user)
        mock_run.assert_awaited_once()


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
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_user_skipped_when_interval_not_elapsed(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """A user whose interval has not elapsed should not be processed."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.heartbeat_frequency = "1h"
        user.preferred_channel = "telegram"
        user.channel_identifier = ""
        user.phone = "+15559990000"

        mock_store = AsyncMock()
        mock_store.list_all.return_value = [user]
        mock_get_store.return_value = mock_store
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
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_user_processed_when_interval_elapsed(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """A user whose interval has elapsed should be processed again."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.heartbeat_frequency = "15m"
        user.preferred_channel = "telegram"
        user.channel_identifier = ""
        user.phone = "+15559990000"

        mock_store = AsyncMock()
        mock_store.list_all.return_value = [user]
        mock_get_store.return_value = mock_store
        mock_run.return_value = None

        scheduler = HeartbeatScheduler()

        # First tick
        await scheduler.tick()
        assert mock_run.await_count == 1

        # Simulate time passing: set last tick to 16 minutes ago
        scheduler._last_tick[1] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=16
        )

        # Second tick: 16 > 15 minutes, so user is due
        await scheduler.tick()
        assert mock_run.await_count == 2

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_invalid_frequency_falls_back_to_global(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Invalid frequency should fall back to global heartbeat_interval_minutes."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5
        mock_settings.heartbeat_interval_minutes = 30

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.heartbeat_frequency = "invalid"
        user.preferred_channel = "telegram"
        user.channel_identifier = ""
        user.phone = "+15559990000"

        mock_store = AsyncMock()
        mock_store.list_all.return_value = [user]
        mock_get_store.return_value = mock_store
        mock_run.return_value = None

        scheduler = HeartbeatScheduler()

        # First tick: always due
        await scheduler.tick()
        assert mock_run.await_count == 1

        # Set last tick to 29 minutes ago (< 30m global default)
        scheduler._last_tick[1] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=29
        )
        await scheduler.tick()
        assert mock_run.await_count == 1  # Not yet due

        # Set last tick to 31 minutes ago (> 30m global default)
        scheduler._last_tick[1] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=31
        )
        await scheduler.tick()
        assert mock_run.await_count == 2  # Now due


# ---------------------------------------------------------------------------
# get_channel_identifier & tick chat_id lookup (#639)
# ---------------------------------------------------------------------------


class TestGetChannelIdentifier:
    """UserStore.get_channel_identifier reverse-index lookup."""

    def test_returns_matching_identifier(self, tmp_path: Path) -> None:
        index_path = tmp_path / "user_index.json"
        index_path.write_text(
            json.dumps({"webchat:web-1": 1, "telegram:99887766": 1}),
            encoding="utf-8",
        )
        store = get_user_store()
        with patch("backend.app.agent.file_store._index_path", return_value=index_path):
            assert store.get_channel_identifier(1, "telegram") == "99887766"

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        index_path = tmp_path / "user_index.json"
        index_path.write_text(
            json.dumps({"webchat:web-1": 1}),
            encoding="utf-8",
        )
        store = get_user_store()
        with patch("backend.app.agent.file_store._index_path", return_value=index_path):
            assert store.get_channel_identifier(1, "telegram") is None

    def test_does_not_return_other_users_identifier(self, tmp_path: Path) -> None:
        index_path = tmp_path / "user_index.json"
        index_path.write_text(
            json.dumps({"telegram:tg-for-a": 1, "webchat:b-1": 2}),
            encoding="utf-8",
        )
        store = get_user_store()
        with patch("backend.app.agent.file_store._index_path", return_value=index_path):
            assert store.get_channel_identifier(2, "telegram") is None


class TestTickChatIdLookup:
    """Heartbeat tick should look up the correct chat_id for the target channel."""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_uses_channel_specific_chat_id(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When falling back to telegram, tick should use the telegram chat_id
        from the user index, not the webchat channel_identifier."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.preferred_channel = "webchat"
        user.channel_identifier = "web-1"
        user.phone = ""

        mock_store = MagicMock()
        mock_store.list_all = AsyncMock(return_value=[user])
        mock_store.get_channel_identifier.return_value = "tg-12345"
        mock_get_store.return_value = mock_store

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_store.get_channel_identifier.assert_called_once_with(1, "telegram")
        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["chat_id"] == "tg-12345"
        assert call_kwargs["channel"] == "telegram"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_user")
    @patch("backend.app.agent.heartbeat.get_user_store")
    @patch("backend.app.agent.heartbeat._pick_heartbeat_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_falls_back_to_channel_identifier(
        self,
        mock_settings: MagicMock,
        mock_pick_channel: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """When no index entry exists, fall back to user.channel_identifier."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        mock_pick_channel.return_value = "telegram"

        user = MagicMock()
        user.id = 1
        user.onboarding_complete = True
        user.preferred_channel = "webchat"
        user.channel_identifier = "web-1"
        user.phone = "+15559990000"

        mock_store = MagicMock()
        mock_store.list_all = AsyncMock(return_value=[user])
        mock_store.get_channel_identifier.return_value = None
        mock_get_store.return_value = mock_store

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args.kwargs
        # Falls back to user.channel_identifier
        assert call_kwargs["chat_id"] == "web-1"
