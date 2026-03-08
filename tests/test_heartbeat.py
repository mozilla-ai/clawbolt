"""Tests for the proactive heartbeat engine."""

from __future__ import annotations

import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage

from backend.app.agent.file_store import (
    ChecklistItem,
    ContractorData,
    HeartbeatLogEntry,
    StoredMessage,
)
from backend.app.agent.heartbeat import (
    COMPOSE_MESSAGE_TOOL,
    CheapCheckResult,
    ComposeMessageParams,
    HeartbeatAction,
    HeartbeatScheduler,
    _is_checklist_item_due,
    _parse_business_hours,
    _parse_tool_call_response,
    _to_local_time,
    build_heartbeat_context,
    evaluate_heartbeat_need,
    get_daily_heartbeat_count,
    is_within_business_hours,
    parse_frequency_to_minutes,
    run_cheap_checks,
    run_heartbeat_for_contractor,
)
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def contractor() -> ContractorData:
    return ContractorData(
        id=1,
        user_id="hb-user-001",
        name="Mike the Plumber",
        phone="+15559990000",
        onboarding_complete=True,
    )


@pytest.fixture()
def contractor_no_hours() -> ContractorData:
    return ContractorData(
        id=2,
        user_id="hb-user-002",
        name="Jane Electric",
        phone="+15559990001",
        onboarding_complete=True,
    )


@pytest.fixture()
def contractor_with_timezone() -> ContractorData:
    return ContractorData(
        id=3,
        user_id="hb-user-003",
        name="Carlos Roofing",
        phone="+15559990002",
        timezone="America/Los_Angeles",
        onboarding_complete=True,
    )


@pytest.fixture()
def mock_messaging() -> MagicMock:
    svc = MagicMock()
    svc.send_text = AsyncMock(return_value="mock_heartbeat_msg_id")
    return svc


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
# Business hours parsing
# ---------------------------------------------------------------------------


class TestParseBusinessHours:
    def test_ampm_simple(self) -> None:
        assert _parse_business_hours("7am-5pm") == (7, 17)

    def test_ampm_with_colons(self) -> None:
        assert _parse_business_hours("7:00am - 5:00pm") == (7, 17)

    def test_24h_format(self) -> None:
        assert _parse_business_hours("08:00-17:00") == (8, 17)

    def test_noon_edge(self) -> None:
        assert _parse_business_hours("12pm-5pm") == (12, 17)

    def test_midnight_edge(self) -> None:
        assert _parse_business_hours("12am-8am") == (0, 8)

    def test_unparseable(self) -> None:
        assert _parse_business_hours("whenever I feel like it") is None


# ---------------------------------------------------------------------------
# is_within_business_hours
# ---------------------------------------------------------------------------


class TestIsWithinBusinessHours:
    @patch("backend.app.agent.heartbeat.settings")
    def test_outside_quiet_hours(
        self, mock_settings: MagicMock, contractor: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 10 AM -- outside quiet hours, should be True
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_inside_quiet_hours_evening(
        self, mock_settings: MagicMock, contractor: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 22:00 -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 22, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_inside_quiet_hours_early_morning(
        self, mock_settings: MagicMock, contractor: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 3 AM -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 3, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False


class TestToLocalTime:
    """Tests for the _to_local_time helper."""

    def test_converts_utc_to_pacific(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        local = _to_local_time(utc_time, "America/Los_Angeles")
        # UTC 17:00 in June (PDT, UTC-7) -> 10:00 local
        assert local.hour == 10

    def test_converts_utc_to_eastern(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        local = _to_local_time(utc_time, "America/New_York")
        # UTC 17:00 in June (EDT, UTC-4) -> 13:00 local
        assert local.hour == 13

    def test_empty_timezone_returns_unchanged(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = _to_local_time(utc_time, "")
        assert result.hour == 17

    def test_invalid_timezone_returns_unchanged(self) -> None:
        utc_time = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = _to_local_time(utc_time, "Not/A_Real_Zone")
        assert result.hour == 17


class TestIsWithinBusinessHoursTimezone:
    """Tests for timezone-correct quiet hour checks."""

    @patch("backend.app.agent.heartbeat.settings")
    def test_timezone_converts_before_quiet_check(
        self, mock_settings: MagicMock, contractor_with_timezone: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 2 PM UTC -> 7 AM Pacific (PDT). Outside quiet hours.
        now = datetime.datetime(2025, 6, 15, 14, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_utc_morning_is_night_in_pacific(
        self, mock_settings: MagicMock, contractor_with_timezone: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 5 AM UTC -> 10 PM Pacific (PDT, previous day). Inside quiet hours.
        now = datetime.datetime(2025, 6, 15, 5, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_no_timezone_uses_utc(
        self, mock_settings: MagicMock, contractor: ContractorData
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # Contractor without timezone uses UTC directly.
        # 10 AM UTC, outside quiet hours.
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    @patch("backend.app.agent.heartbeat.settings")
    def test_invalid_timezone_falls_back_to_utc(self, mock_settings: MagicMock) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        c = ContractorData(
            id=99,
            user_id="hb-user-bad-tz",
            name="Bad TZ",
            timezone="Invalid/Timezone",
            onboarding_complete=True,
        )
        # 10 AM UTC -> falls back to UTC -> outside quiet hours
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(c, now) is True


# ---------------------------------------------------------------------------
# Cheap checks
# ---------------------------------------------------------------------------


class TestRunCheapChecks:
    @pytest.mark.asyncio
    async def test_no_flags_when_clean(self, contractor: ContractorData) -> None:
        """No stale estimates, no checklist items, no time-sensitive memories."""
        result = await run_cheap_checks(contractor)
        assert not result.has_flags
        assert result.flags == []

    @pytest.mark.asyncio
    async def test_stale_estimate_flagged(self, contractor: ContractorData) -> None:
        """Draft estimate older than 24h should be flagged."""
        from backend.app.agent.file_store import EstimateStore

        store = EstimateStore(contractor.id)
        est = await store.create(
            description="Deck build for Smith",
            total_amount=3000,
            status="draft",
        )
        # Backdate created_at by rewriting the file
        from backend.app.agent.file_store import _write_json

        est.created_at = datetime.datetime(2025, 6, 13, 8, 0, tzinfo=datetime.UTC).isoformat()
        _write_json(store._estimate_path(est.id), est.model_dump())

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        result = await run_cheap_checks(contractor, now=now)
        assert result.has_flags
        assert len(result.stale_estimates) == 1
        assert "Deck build" in result.flags[0]

    @pytest.mark.asyncio
    async def test_recent_draft_not_flagged(self, contractor: ContractorData) -> None:
        """Draft estimate less than 24h old should NOT be flagged."""
        from backend.app.agent.file_store import EstimateStore, _write_json

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        store = EstimateStore(contractor.id)
        est = await store.create(
            description="Fresh estimate",
            total_amount=1000,
            status="draft",
        )
        est.created_at = (now - datetime.timedelta(hours=12)).isoformat()
        _write_json(store._estimate_path(est.id), est.model_dump())

        result = await run_cheap_checks(contractor, now=now)
        assert not result.has_flags
        assert len(result.stale_estimates) == 0

    @pytest.mark.asyncio
    async def test_sent_estimate_not_flagged(self, contractor: ContractorData) -> None:
        """Sent estimates should not be flagged regardless of age."""
        from backend.app.agent.file_store import EstimateStore, _write_json

        store = EstimateStore(contractor.id)
        est = await store.create(
            description="Already sent",
            total_amount=5000,
            status="sent",
        )
        est.created_at = datetime.datetime(2025, 6, 10, 8, 0, tzinfo=datetime.UTC).isoformat()
        _write_json(store._estimate_path(est.id), est.model_dump())

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        result = await run_cheap_checks(contractor, now=now)
        assert not result.has_flags

    @pytest.mark.asyncio
    async def test_checklist_item_due(self, contractor: ContractorData) -> None:
        """Active checklist item with no last_triggered_at should be flagged."""
        from backend.app.agent.file_store import HeartbeatStore

        store = HeartbeatStore(contractor.id)
        await store.add_checklist_item(
            description="Check material prices",
            schedule="daily",
        )

        result = await run_cheap_checks(contractor)
        assert result.has_flags
        assert len(result.due_checklist_items) == 1
        assert "material prices" in result.flags[0]

    @pytest.mark.asyncio
    async def test_checklist_item_paused_not_flagged(self, contractor: ContractorData) -> None:
        """Paused checklist items should not be flagged."""
        from backend.app.agent.file_store import HeartbeatStore

        store = HeartbeatStore(contractor.id)
        item = await store.add_checklist_item(
            description="Paused item",
            schedule="daily",
        )
        await store.update_checklist_item(item.id, status="paused")

        result = await run_cheap_checks(contractor)
        assert not result.has_flags

    @pytest.mark.asyncio
    async def test_time_sensitive_memory_flagged(self, contractor: ContractorData) -> None:
        """Memory facts with time-sensitive keywords should be flagged."""
        from backend.app.agent.file_store import get_memory_store

        store = get_memory_store(contractor.id)
        await store.save_memory(
            key="smith_followup",
            value="Follow up with Smith about deck quote",
            category="client",
        )

        result = await run_cheap_checks(contractor)
        assert result.has_flags
        assert len(result.time_sensitive_memories) == 1
        assert "smith_followup" in result.flags[0]

    @pytest.mark.asyncio
    async def test_regular_memory_not_flagged(self, contractor: ContractorData) -> None:
        """Memory facts without time-sensitive keywords should not be flagged."""
        from backend.app.agent.file_store import get_memory_store

        store = get_memory_store(contractor.id)
        await store.save_memory(
            key="kitchen_rate",
            value="Standard kitchen remodel rate is $150/hour",
            category="pricing",
        )

        result = await run_cheap_checks(contractor)
        assert not result.has_flags

    @pytest.mark.asyncio
    async def test_multiple_flags_combined(self, contractor: ContractorData) -> None:
        """Multiple issues should produce multiple flags."""
        from backend.app.agent.file_store import EstimateStore, HeartbeatStore, _write_json

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)

        # Stale estimate
        est_store = EstimateStore(contractor.id)
        est = await est_store.create(
            description="Old estimate",
            total_amount=2000,
            status="draft",
        )
        est.created_at = (now - datetime.timedelta(hours=48)).isoformat()
        _write_json(est_store._estimate_path(est.id), est.model_dump())

        # Due checklist item
        hb_store = HeartbeatStore(contractor.id)
        await hb_store.add_checklist_item(
            description="Check inbox",
            schedule="daily",
        )

        result = await run_cheap_checks(contractor, now=now)
        assert result.has_flags
        assert len(result.flags) == 2

    @pytest.mark.asyncio
    async def test_idle_contractor_flagged(self, contractor: ContractorData) -> None:
        """Contractor with last inbound message older than idle_days should be flagged."""
        from backend.app.agent.file_store import get_session_store

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        store = get_session_store(contractor.id)

        session, _ = await store.get_or_create_session()
        # Write a backdated inbound message directly to the JSONL file
        from backend.app.agent.file_store import _append_jsonl

        msg = StoredMessage(
            direction="inbound",
            body="Need a quote",
            timestamp=(now - datetime.timedelta(days=5)).isoformat(),
            seq=1,
        )
        _append_jsonl(store._session_path(session.session_id), msg.model_dump())

        result = await run_cheap_checks(contractor, now=now)
        assert result.has_flags
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "5 days" in idle_flags[0]

    @pytest.mark.asyncio
    async def test_active_contractor_not_flagged_idle(self, contractor: ContractorData) -> None:
        """Contractor with recent inbound message should not be flagged as idle."""
        from backend.app.agent.file_store import _append_jsonl, get_session_store

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        store = get_session_store(contractor.id)

        session, _ = await store.get_or_create_session()
        msg = StoredMessage(
            direction="inbound",
            body="Just checking in",
            timestamp=(now - datetime.timedelta(hours=12)).isoformat(),
            seq=1,
        )
        _append_jsonl(store._session_path(session.session_id), msg.model_dump())

        result = await run_cheap_checks(contractor, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 0

    @pytest.mark.asyncio
    async def test_no_messages_old_contractor_flagged(self) -> None:
        """Contractor with no messages who was created more than idle_days ago should be flagged."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        c = ContractorData(
            id=50,
            user_id="hb-idle-001",
            name="Old Timer",
            phone="+15559990099",
            onboarding_complete=True,
            created_at=now - datetime.timedelta(days=7),
        )

        result = await run_cheap_checks(c, now=now)
        assert result.has_flags
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "onboarding" in idle_flags[0]

    @pytest.mark.asyncio
    async def test_no_messages_new_contractor_not_flagged(self) -> None:
        """Contractor with no messages who just onboarded should not be flagged as idle."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        c = ContractorData(
            id=51,
            user_id="hb-new-001",
            name="Fresh Start",
            phone="+15559990098",
            onboarding_complete=True,
            created_at=now - datetime.timedelta(hours=6),
        )

        result = await run_cheap_checks(c, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 0

    @pytest.mark.asyncio
    async def test_outbound_only_still_flagged_idle(self, contractor: ContractorData) -> None:
        """Contractor with only outbound messages (no inbound) should check created_at."""
        from backend.app.agent.file_store import _append_jsonl, get_session_store

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        # Backdate the contractor's created_at
        contractor.created_at = now - datetime.timedelta(days=5)

        store = get_session_store(contractor.id)
        session, _ = await store.get_or_create_session()
        msg = StoredMessage(
            direction="outbound",
            body="Welcome!",
            timestamp=(now - datetime.timedelta(days=4)).isoformat(),
            seq=1,
        )
        _append_jsonl(store._session_path(session.session_id), msg.model_dump())

        result = await run_cheap_checks(contractor, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "onboarding" in idle_flags[0]


# ---------------------------------------------------------------------------
# Checklist item due logic
# ---------------------------------------------------------------------------


class TestIsChecklistItemDue:
    def test_never_triggered(self) -> None:
        """Item that has never been triggered is always due."""
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=None,
        )
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert _is_checklist_item_due(item, now) is True

    def test_daily_recently_triggered(self) -> None:
        """Daily item triggered 2 hours ago should not be due."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=(now - datetime.timedelta(hours=2)).isoformat(),
        )
        assert _is_checklist_item_due(item, now) is False

    def test_daily_triggered_yesterday(self) -> None:
        """Daily item triggered 24 hours ago should be due."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=(now - datetime.timedelta(hours=24)).isoformat(),
        )
        assert _is_checklist_item_due(item, now) is True

    def test_once_already_triggered(self) -> None:
        """Once-scheduled item that was already triggered is never due again."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="once",
            last_triggered_at=(now - datetime.timedelta(hours=1)).isoformat(),
        )
        assert _is_checklist_item_due(item, now) is False

    def test_weekdays_on_saturday(self) -> None:
        """Weekday item should not fire on Saturday."""
        # 2025-06-14 is a Saturday
        saturday = datetime.datetime(2025, 6, 14, 10, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="weekdays",
            last_triggered_at=None,
        )
        assert _is_checklist_item_due(item, saturday) is False

    def test_weekdays_on_monday(self) -> None:
        """Weekday item should fire on Monday if not triggered recently."""
        # 2025-06-16 is a Monday
        monday = datetime.datetime(2025, 6, 16, 10, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="weekdays",
            last_triggered_at=None,
        )
        assert _is_checklist_item_due(item, monday) is True

    def test_naive_last_triggered_at(self) -> None:
        """Timezone-naive last_triggered_at (as ISO string) should not raise TypeError."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        # Simulate naive datetime as ISO string (no tz info)
        naive_last = "2025-06-14T08:00:00"
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=naive_last,
        )
        # Should not raise TypeError and should be due (>20h elapsed)
        assert _is_checklist_item_due(item, now) is True

    def test_weekdays_saturday_utc_but_friday_local(self) -> None:
        """Weekday item should fire when UTC is Saturday but local time is still Friday.

        Regression test: before this fix, _is_checklist_item_due checked
        now.weekday() in UTC. A contractor in America/Los_Angeles at 5 PM
        Friday Pacific (00:00 Saturday UTC) would have the weekday gate
        incorrectly skip the item.
        """
        # Saturday 00:00 UTC = Friday 5:00 PM Pacific (PDT, UTC-7)
        saturday_utc = datetime.datetime(2025, 6, 14, 0, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Weekly check-in",
            schedule="weekdays",
            last_triggered_at=None,
        )
        # Without timezone: UTC says Saturday -> should skip (old buggy behavior)
        assert _is_checklist_item_due(item, saturday_utc) is False
        # With timezone: local is Friday -> should fire (fixed behavior)
        assert _is_checklist_item_due(item, saturday_utc, tz_name="America/Los_Angeles") is True

    def test_weekdays_sunday_local_still_skipped(self) -> None:
        """Weekday item should still be skipped on a genuine local Sunday."""
        # Sunday 10 AM Pacific = Sunday 5 PM UTC
        sunday_utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        item = ChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="weekdays",
            last_triggered_at=None,
        )
        assert _is_checklist_item_due(item, sunday_utc, tz_name="America/Los_Angeles") is False


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
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_no(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action",
            message="",
            reasoning="Nothing actionable",
            priority=1,
        )
        action = await evaluate_heartbeat_need(contractor, ["Stale draft estimate"])
        assert action.action_type == "no_action"
        assert action.message == ""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_send(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="send_message",
            message="Hey Mike, you have a draft estimate sitting for 2 days.",
            reasoning="Stale draft estimate",
            priority=4,
        )
        action = await evaluate_heartbeat_need(contractor, ["Stale draft estimate"])
        assert action.action_type == "send_message"
        assert "draft estimate" in action.message

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_uses_heartbeat_model_when_set(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        """When heartbeat_model is configured, it should be used instead of llm_model."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = "gpt-4o-mini"
        mock_settings.heartbeat_provider = "openai"
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="", priority=1
        )
        await evaluate_heartbeat_need(contractor, ["test flag"])

        # Verify the cheap model was used
        call_kwargs = mock_llm.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_api_base_not_api_key(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        """Regression test: acompletion must receive api_base, not api_key."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = "http://localhost:1234/v1"
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
        await evaluate_heartbeat_need(contractor, ["test flag"])
        _, kwargs = mock_llm.call_args
        assert "api_base" in kwargs
        assert kwargs["api_base"] == "http://localhost:1234/v1"
        assert "api_key" not in kwargs

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_text_response_falls_back_to_no_action(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        """If LLM returns text instead of tool call, default to no_action."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = make_text_response("I'm not sure what to do {broken json")
        action = await evaluate_heartbeat_need(contractor, ["test flag"])
        assert action.action_type == "no_action"
        assert action.priority == 0

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_tools_to_acompletion(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        """acompletion should receive tools=[COMPOSE_MESSAGE_TOOL]."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
        await evaluate_heartbeat_need(contractor, ["test flag"])
        _, kwargs = mock_llm.call_args
        assert "tools" in kwargs
        assert kwargs["tools"] == [COMPOSE_MESSAGE_TOOL]

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_prompt_does_not_ask_for_raw_json(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        contractor: ContractorData,
    ) -> None:
        """System prompt should not contain 'Respond with ONLY a JSON object'."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = _make_heartbeat_tool_call(
            action="no_action", message="", reasoning="test", priority=1
        )
        await evaluate_heartbeat_need(contractor, ["test flag"])
        call_args = mock_llm.call_args
        system_content = call_args.kwargs["system"]
        assert "Respond with ONLY a JSON object" not in system_content
        assert "compose_message" in system_content


# ---------------------------------------------------------------------------
# run_heartbeat_for_contractor
# ---------------------------------------------------------------------------


class TestRunHeartbeatForContractor:
    @pytest.mark.asyncio
    async def test_skip_not_onboarded(self, mock_messaging: MagicMock) -> None:
        c = ContractorData(id=10, user_id="hb-new", phone="+15550000000", onboarding_complete=False)
        result = await run_heartbeat_for_contractor(c, mock_messaging, 5)
        assert result is None
        mock_messaging.send_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=False)
    async def test_skip_outside_hours(
        self,
        _mock_hours: MagicMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        result = await run_heartbeat_for_contractor(contractor, mock_messaging, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.get_daily_heartbeat_count")
    async def test_skip_rate_limited(
        self,
        mock_count: AsyncMock,
        _mock_hours: MagicMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        mock_count.return_value = 5
        result = await run_heartbeat_for_contractor(contractor, mock_messaging, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_no_action_when_checks_clean(
        self,
        _mock_hours: MagicMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        """When cheap checks return no flags, LLM is skipped and no message sent."""
        result = await run_heartbeat_for_contractor(contractor, mock_messaging, 5)
        assert result is not None
        assert result.action_type == "no_action"
        assert "cheap checks clean" in result.reasoning
        mock_messaging.send_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_send_message_and_record(
        self,
        _mock_hours: MagicMock,
        mock_checks: AsyncMock,
        mock_eval: AsyncMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        """When cheap checks flag something and LLM says send, message is delivered."""
        check_result = CheapCheckResult(
            flags=["Stale draft estimate"],
        )
        mock_checks.return_value = check_result
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Reminder: draft estimate pending!",
            reasoning="Stale draft",
            priority=4,
        )
        result = await run_heartbeat_for_contractor(contractor, mock_messaging, 5)

        assert result is not None
        assert result.action_type == "send_message"
        mock_messaging.send_text.assert_awaited_once_with(
            to=contractor.phone, body="Reminder: draft estimate pending!"
        )
        # LLM was called with the flags
        mock_eval.assert_awaited_once_with(
            contractor, ["Stale draft estimate"], messaging_service=mock_messaging
        )

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_sms_failure_graceful(
        self,
        _mock_hours: MagicMock,
        mock_checks: AsyncMock,
        mock_eval: AsyncMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        mock_checks.return_value = CheapCheckResult(flags=["test flag"])
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Reminder!",
            reasoning="test",
            priority=3,
        )
        mock_messaging.send_text = AsyncMock(side_effect=Exception("Messaging service down"))
        result = await run_heartbeat_for_contractor(contractor, mock_messaging, 5)
        # Should still return the action, just not record a message
        assert result is not None
        assert result.action_type == "send_message"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_checklist_items_marked_triggered(
        self,
        _mock_hours: MagicMock,
        mock_checks: AsyncMock,
        mock_eval: AsyncMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        """After sending a message, due checklist items should be marked as triggered."""
        from backend.app.agent.file_store import HeartbeatStore

        # Write the item to disk so the runner can update it
        hb_store = HeartbeatStore(contractor.id)
        item = await hb_store.add_checklist_item(
            description="Check inbox",
            schedule="daily",
        )

        mock_checks.return_value = CheapCheckResult(
            flags=["Checklist item due: Check inbox"],
            due_checklist_items=[item],
        )
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Time to check your inbox!",
            reasoning="checklist",
            priority=3,
        )
        await run_heartbeat_for_contractor(contractor, mock_messaging, 5)

        # Read back from disk to verify the item was updated
        updated_items = await hb_store.get_checklist()
        updated_item = updated_items[0]
        assert updated_item.last_triggered_at is not None
        assert updated_item.status == "active"  # daily stays active

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_once_item_completed_after_trigger(
        self,
        _mock_hours: MagicMock,
        mock_checks: AsyncMock,
        mock_eval: AsyncMock,
        contractor: ContractorData,
        mock_messaging: MagicMock,
    ) -> None:
        """A once-scheduled checklist item should be marked completed after triggering."""
        from backend.app.agent.file_store import HeartbeatStore

        # Write the item to disk so the runner can update it
        hb_store = HeartbeatStore(contractor.id)
        item = await hb_store.add_checklist_item(
            description="Remind about meeting",
            schedule="once",
        )

        mock_checks.return_value = CheapCheckResult(
            flags=["Checklist item due: Remind about meeting"],
            due_checklist_items=[item],
        )
        mock_eval.return_value = HeartbeatAction(
            action_type="send_message",
            message="Don't forget your meeting!",
            reasoning="once item",
            priority=4,
        )
        await run_heartbeat_for_contractor(contractor, mock_messaging, 5)

        # Read back from disk to verify the item was updated
        updated_items = await hb_store.get_checklist()
        updated_item = updated_items[0]
        assert updated_item.last_triggered_at is not None
        assert updated_item.status == "completed"


# ---------------------------------------------------------------------------
# get_daily_heartbeat_count (persistent rate limiting)
# ---------------------------------------------------------------------------


class TestGetDailyHeartbeatCount:
    @pytest.mark.asyncio
    async def test_zero_when_no_logs(self, contractor: ContractorData) -> None:
        assert await get_daily_heartbeat_count(contractor.id) == 0

    @pytest.mark.asyncio
    async def test_counts_today_only(self, contractor: ContractorData) -> None:
        """Logs from yesterday should not count toward today's limit."""
        from backend.app.agent.file_store import HeartbeatStore, _append_jsonl

        store = HeartbeatStore(contractor.id)
        # Add a log from today
        await store.log_heartbeat()
        # Add a log from yesterday directly to the JSONL file
        yesterday = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        entry = HeartbeatLogEntry(contractor_id=contractor.id, created_at=yesterday.isoformat())
        _append_jsonl(store._log_path, entry.model_dump())

        assert await get_daily_heartbeat_count(contractor.id) == 1

    @pytest.mark.asyncio
    async def test_counts_multiple_today(self, contractor: ContractorData) -> None:
        from backend.app.agent.file_store import HeartbeatStore

        store = HeartbeatStore(contractor.id)
        for _ in range(3):
            await store.log_heartbeat()

        assert await get_daily_heartbeat_count(contractor.id) == 3

    @pytest.mark.asyncio
    async def test_scoped_to_contractor(self, contractor: ContractorData) -> None:
        """Logs from other contractors should not count."""
        from backend.app.agent.file_store import HeartbeatStore

        other = ContractorData(
            id=60,
            user_id="hb-other",
            phone="+15551112222",
            onboarding_complete=True,
        )

        other_store = HeartbeatStore(other.id)
        await other_store.log_heartbeat()

        assert await get_daily_heartbeat_count(contractor.id) == 0
        assert await get_daily_heartbeat_count(other.id) == 1


# ---------------------------------------------------------------------------
# build_heartbeat_context
# ---------------------------------------------------------------------------


class TestBuildHeartbeatContext:
    @pytest.mark.asyncio
    async def test_includes_profile_and_flags(self, contractor: ContractorData) -> None:
        from backend.app.agent.file_store import get_session_store
        from backend.app.enums import MessageDirection

        # Add a session with a message so context builder works
        store = get_session_store(contractor.id)
        session, _ = await store.get_or_create_session()
        await store.add_message(
            session=session,
            direction=MessageDirection.INBOUND,
            body="I need a quote",
        )

        flags = ["Stale draft estimate: Kitchen remodel"]
        prompt = await build_heartbeat_context(contractor, flags)

        # build_heartbeat_context now returns a full prompt string
        assert "Plumber" in prompt
        assert "Kitchen remodel" in prompt
        assert "I need a quote" in prompt


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
    @patch("backend.app.agent.heartbeat.get_contractor_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_queries_onboarded(
        self, mock_messaging_cls: MagicMock, mock_get_store: MagicMock
    ) -> None:
        """Tick should query all contractors via list_all and filter by onboarding_complete."""
        mock_store = AsyncMock()
        mock_store.list_all.return_value = []
        mock_get_store.return_value = mock_store

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_store.list_all.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.get_contractor_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_concurrent_processing(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """tick() should process multiple contractors concurrently."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create mock contractors
        contractors = []
        for i in range(4):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            contractors.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = contractors
        mock_get_store.return_value = mock_store

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # run_heartbeat_for_contractor called once per contractor
        assert mock_run.await_count == len(contractors)

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.get_contractor_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_error_isolation(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """One contractor failure should not prevent others from being processed."""
        mock_settings.heartbeat_concurrency = 5
        mock_settings.heartbeat_max_daily_messages = 5

        contractors = []
        for i in range(3):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            contractors.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = contractors
        mock_get_store.return_value = mock_store

        # Second contractor raises, others succeed
        mock_run.side_effect = [
            HeartbeatAction("no_action", "", "clean", 0),
            RuntimeError("LLM timeout"),
            HeartbeatAction("no_action", "", "clean", 0),
        ]

        scheduler = HeartbeatScheduler()
        # Should not raise despite one contractor failing
        await scheduler.tick()

        # All three were attempted
        assert mock_run.await_count == 3

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.get_contractor_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_semaphore_limits_concurrency(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_get_store: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """Semaphore should limit the number of concurrent contractor evaluations."""
        concurrency_limit = 2
        mock_settings.heartbeat_concurrency = concurrency_limit
        mock_settings.heartbeat_max_daily_messages = 5

        contractors = []
        for i in range(5):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            c.preferred_channel = "telegram"
            contractors.append(c)

        mock_store = AsyncMock()
        mock_store.list_all.return_value = contractors
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
    @patch("backend.app.agent.heartbeat.get_contractor_store")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_no_contractors(
        self, mock_messaging_cls: MagicMock, mock_get_store: MagicMock
    ) -> None:
        """tick() with no onboarded contractors should return early."""
        mock_store = AsyncMock()
        mock_store.list_all.return_value = []
        mock_get_store.return_value = mock_store

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_store.list_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# parse_frequency_to_minutes
# ---------------------------------------------------------------------------


class TestParseFrequencyToMinutes:
    def test_empty_string_returns_none(self) -> None:
        assert parse_frequency_to_minutes("") is None

    def test_whitespace_returns_none(self) -> None:
        assert parse_frequency_to_minutes("   ") is None

    def test_daily_keyword(self) -> None:
        assert parse_frequency_to_minutes("daily") == 1440

    def test_daily_case_insensitive(self) -> None:
        assert parse_frequency_to_minutes("Daily") == 1440
        assert parse_frequency_to_minutes("DAILY") == 1440

    def test_minutes_short(self) -> None:
        assert parse_frequency_to_minutes("30m") == 30

    def test_hours_short(self) -> None:
        assert parse_frequency_to_minutes("1h") == 60
        assert parse_frequency_to_minutes("2h") == 120

    def test_days_short(self) -> None:
        assert parse_frequency_to_minutes("1d") == 1440
        assert parse_frequency_to_minutes("2d") == 2880

    def test_long_form_minutes(self) -> None:
        assert parse_frequency_to_minutes("45minutes") == 45
        assert parse_frequency_to_minutes("1minute") == 1

    def test_long_form_hours(self) -> None:
        assert parse_frequency_to_minutes("3hours") == 180
        assert parse_frequency_to_minutes("1hour") == 60

    def test_long_form_days(self) -> None:
        assert parse_frequency_to_minutes("1day") == 1440
        assert parse_frequency_to_minutes("2days") == 2880

    def test_invalid_returns_none(self) -> None:
        assert parse_frequency_to_minutes("never") is None
        assert parse_frequency_to_minutes("weekly") is None
