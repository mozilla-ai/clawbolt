"""Tests for the proactive heartbeat engine."""

from __future__ import annotations

import datetime
import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from any_llm.types.messages import MessageContentBlock, MessageResponse, MessageUsage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

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
from backend.app.database import Base
from backend.app.models import (
    Contractor,
    Conversation,
    Estimate,
    HeartbeatChecklistItem,
    HeartbeatLog,
    Memory,
    Message,
)
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Generator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def contractor(db: Session) -> Contractor:
    c = Contractor(
        user_id="hb-user-001",
        name="Mike the Plumber",
        phone="+15559990000",
        trade="Plumber",
        location="Portland, OR",
        business_hours="7am-5pm",
        onboarding_complete=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture()
def contractor_no_hours(db: Session) -> Contractor:
    c = Contractor(
        user_id="hb-user-002",
        name="Jane Electric",
        phone="+15559990001",
        trade="Electrician",
        onboarding_complete=True,
        business_hours="",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@pytest.fixture()
def contractor_with_timezone(db: Session) -> Contractor:
    c = Contractor(
        user_id="hb-user-003",
        name="Carlos Roofing",
        phone="+15559990002",
        trade="Roofer",
        location="Los Angeles, CA",
        business_hours="7am-5pm",
        timezone="America/Los_Angeles",
        onboarding_complete=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


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
    def test_within_hours(self, contractor: Contractor) -> None:
        # 10 AM -- within 7am-5pm
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    def test_before_hours(self, contractor: Contractor) -> None:
        # 5 AM -- before 7am-5pm
        now = datetime.datetime(2025, 6, 15, 5, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    def test_after_hours(self, contractor: Contractor) -> None:
        # 8 PM -- after 7am-5pm
        now = datetime.datetime(2025, 6, 15, 20, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    def test_at_boundary_start(self, contractor: Contractor) -> None:
        # Exactly 7 AM -- should be within
        now = datetime.datetime(2025, 6, 15, 7, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    def test_at_boundary_end(self, contractor: Contractor) -> None:
        # Exactly 5 PM (17:00) -- should be outside (end is exclusive)
        now = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_fallback_quiet_hours(
        self, mock_settings: MagicMock, contractor_no_hours: Contractor
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 10 AM -- outside quiet hours, should be True
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is True

        # 22:00 -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 22, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is False

        # 3 AM -- inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 3, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is False


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
    """Tests for timezone-correct business hour checks."""

    def test_utc_afternoon_is_morning_in_pacific(
        self, contractor_with_timezone: Contractor
    ) -> None:
        # 2 PM UTC -> 7 AM Pacific (PDT). Business hours are 7am-5pm, so within.
        now = datetime.datetime(2025, 6, 15, 14, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is True

    def test_utc_evening_is_afternoon_in_pacific(
        self, contractor_with_timezone: Contractor
    ) -> None:
        # 11 PM UTC -> 4 PM Pacific (PDT). Business hours 7am-5pm, so within.
        now = datetime.datetime(2025, 6, 15, 23, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is True

    def test_utc_midnight_is_evening_in_pacific(self, contractor_with_timezone: Contractor) -> None:
        # 1 AM UTC (next day) -> 6 PM Pacific (PDT). After 5pm, so outside.
        now = datetime.datetime(2025, 6, 16, 1, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is False

    def test_utc_morning_is_night_in_pacific(self, contractor_with_timezone: Contractor) -> None:
        # 5 AM UTC -> 10 PM Pacific (PDT, previous day). Outside 7am-5pm.
        now = datetime.datetime(2025, 6, 15, 5, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_with_timezone, now) is False

    def test_no_timezone_uses_utc(self, contractor: Contractor) -> None:
        # Contractor without timezone field set uses UTC directly.
        # 10 AM UTC, business hours 7am-5pm -> within.
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    def test_invalid_timezone_falls_back_to_utc(self, db: Session) -> None:
        c = Contractor(
            user_id="hb-user-bad-tz",
            name="Bad TZ",
            trade="Plumber",
            business_hours="7am-5pm",
            timezone="Invalid/Timezone",
            onboarding_complete=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        # 10 AM UTC -> falls back to UTC -> within 7am-5pm
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(c, now) is True


# ---------------------------------------------------------------------------
# Cheap checks
# ---------------------------------------------------------------------------


class TestRunCheapChecks:
    def test_no_flags_when_clean(self, db: Session, contractor: Contractor) -> None:
        """No stale estimates, no checklist items, no time-sensitive memories."""
        result = run_cheap_checks(db, contractor)
        assert not result.has_flags
        assert result.flags == []

    def test_stale_estimate_flagged(self, db: Session, contractor: Contractor) -> None:
        """Draft estimate older than 24h should be flagged."""
        est = Estimate(
            contractor_id=contractor.id,
            description="Deck build for Smith",
            total_amount=3000,
            status="draft",
            created_at=datetime.datetime(2025, 6, 13, 8, 0, tzinfo=datetime.UTC),
        )
        db.add(est)
        db.commit()

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        result = run_cheap_checks(db, contractor, now=now)
        assert result.has_flags
        assert len(result.stale_estimates) == 1
        assert "Deck build" in result.flags[0]

    def test_recent_draft_not_flagged(self, db: Session, contractor: Contractor) -> None:
        """Draft estimate less than 24h old should NOT be flagged."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        est = Estimate(
            contractor_id=contractor.id,
            description="Fresh estimate",
            total_amount=1000,
            status="draft",
            created_at=now - datetime.timedelta(hours=12),
        )
        db.add(est)
        db.commit()

        result = run_cheap_checks(db, contractor, now=now)
        assert not result.has_flags
        assert len(result.stale_estimates) == 0

    def test_sent_estimate_not_flagged(self, db: Session, contractor: Contractor) -> None:
        """Sent estimates should not be flagged regardless of age."""
        est = Estimate(
            contractor_id=contractor.id,
            description="Already sent",
            total_amount=5000,
            status="sent",
            created_at=datetime.datetime(2025, 6, 10, 8, 0, tzinfo=datetime.UTC),
        )
        db.add(est)
        db.commit()

        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        result = run_cheap_checks(db, contractor, now=now)
        assert not result.has_flags

    def test_checklist_item_due(self, db: Session, contractor: Contractor) -> None:
        """Active checklist item with no last_triggered_at should be flagged."""
        item = HeartbeatChecklistItem(
            contractor_id=contractor.id,
            description="Check material prices",
            schedule="daily",
        )
        db.add(item)
        db.commit()

        result = run_cheap_checks(db, contractor)
        assert result.has_flags
        assert len(result.due_checklist_items) == 1
        assert "material prices" in result.flags[0]

    def test_checklist_item_paused_not_flagged(self, db: Session, contractor: Contractor) -> None:
        """Paused checklist items should not be flagged."""
        item = HeartbeatChecklistItem(
            contractor_id=contractor.id,
            description="Paused item",
            schedule="daily",
            status="paused",
        )
        db.add(item)
        db.commit()

        result = run_cheap_checks(db, contractor)
        assert not result.has_flags

    def test_time_sensitive_memory_flagged(self, db: Session, contractor: Contractor) -> None:
        """Memory facts with time-sensitive keywords should be flagged."""
        mem = Memory(
            contractor_id=contractor.id,
            key="smith_followup",
            value="Follow up with Smith about deck quote",
            category="client",
        )
        db.add(mem)
        db.commit()

        result = run_cheap_checks(db, contractor)
        assert result.has_flags
        assert len(result.time_sensitive_memories) == 1
        assert "smith_followup" in result.flags[0]

    def test_regular_memory_not_flagged(self, db: Session, contractor: Contractor) -> None:
        """Memory facts without time-sensitive keywords should not be flagged."""
        mem = Memory(
            contractor_id=contractor.id,
            key="kitchen_rate",
            value="Standard kitchen remodel rate is $150/hour",
            category="pricing",
        )
        db.add(mem)
        db.commit()

        result = run_cheap_checks(db, contractor)
        assert not result.has_flags

    def test_multiple_flags_combined(self, db: Session, contractor: Contractor) -> None:
        """Multiple issues should produce multiple flags."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)

        # Stale estimate
        est = Estimate(
            contractor_id=contractor.id,
            description="Old estimate",
            total_amount=2000,
            status="draft",
            created_at=now - datetime.timedelta(hours=48),
        )
        db.add(est)

        # Due checklist item
        item = HeartbeatChecklistItem(
            contractor_id=contractor.id,
            description="Check inbox",
            schedule="daily",
        )
        db.add(item)
        db.commit()

        result = run_cheap_checks(db, contractor, now=now)
        assert result.has_flags
        assert len(result.flags) == 2

    def test_idle_contractor_flagged(self, db: Session, contractor: Contractor) -> None:
        """Contractor with last inbound message older than idle_days should be flagged."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            direction="inbound",
            body="Need a quote",
            created_at=now - datetime.timedelta(days=5),
        )
        db.add(msg)
        db.commit()

        result = run_cheap_checks(db, contractor, now=now)
        assert result.has_flags
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "5 days" in idle_flags[0]

    def test_active_contractor_not_flagged_idle(self, db: Session, contractor: Contractor) -> None:
        """Contractor with recent inbound message should not be flagged as idle."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            direction="inbound",
            body="Just checking in",
            created_at=now - datetime.timedelta(hours=12),
        )
        db.add(msg)
        db.commit()

        result = run_cheap_checks(db, contractor, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 0

    def test_no_messages_old_contractor_flagged(self, db: Session) -> None:
        """Contractor with no messages who was created more than idle_days ago should be flagged."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        c = Contractor(
            user_id="hb-idle-001",
            name="Old Timer",
            phone="+15559990099",
            trade="Carpenter",
            onboarding_complete=True,
            created_at=now - datetime.timedelta(days=7),
        )
        db.add(c)
        db.commit()
        db.refresh(c)

        result = run_cheap_checks(db, c, now=now)
        assert result.has_flags
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "onboarding" in idle_flags[0]

    def test_no_messages_new_contractor_not_flagged(self, db: Session) -> None:
        """Contractor with no messages who just onboarded should not be flagged as idle."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        c = Contractor(
            user_id="hb-new-001",
            name="Fresh Start",
            phone="+15559990098",
            trade="Plumber",
            onboarding_complete=True,
            created_at=now - datetime.timedelta(hours=6),
        )
        db.add(c)
        db.commit()
        db.refresh(c)

        result = run_cheap_checks(db, c, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 0

    def test_outbound_only_still_flagged_idle(self, db: Session, contractor: Contractor) -> None:
        """Contractor with only outbound messages (no inbound) should check created_at."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        # Backdate the contractor's created_at
        contractor.created_at = now - datetime.timedelta(days=5)
        db.commit()

        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        msg = Message(
            conversation_id=conv.id,
            direction="outbound",
            body="Welcome!",
            created_at=now - datetime.timedelta(days=4),
        )
        db.add(msg)
        db.commit()

        result = run_cheap_checks(db, contractor, now=now)
        idle_flags = [f for f in result.flags if "idle" in f.lower()]
        assert len(idle_flags) == 1
        assert "onboarding" in idle_flags[0]


# ---------------------------------------------------------------------------
# Checklist item due logic
# ---------------------------------------------------------------------------


class TestIsChecklistItemDue:
    def test_never_triggered(self) -> None:
        """Item that has never been triggered is always due."""
        item = HeartbeatChecklistItem(
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
        item = HeartbeatChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=now - datetime.timedelta(hours=2),
        )
        assert _is_checklist_item_due(item, now) is False

    def test_daily_triggered_yesterday(self) -> None:
        """Daily item triggered 24 hours ago should be due."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        item = HeartbeatChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=now - datetime.timedelta(hours=24),
        )
        assert _is_checklist_item_due(item, now) is True

    def test_once_already_triggered(self) -> None:
        """Once-scheduled item that was already triggered is never due again."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        item = HeartbeatChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="once",
            last_triggered_at=now - datetime.timedelta(hours=1),
        )
        assert _is_checklist_item_due(item, now) is False

    def test_weekdays_on_saturday(self) -> None:
        """Weekday item should not fire on Saturday."""
        # 2025-06-14 is a Saturday
        saturday = datetime.datetime(2025, 6, 14, 10, 0, tzinfo=datetime.UTC)
        item = HeartbeatChecklistItem(
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
        item = HeartbeatChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="weekdays",
            last_triggered_at=None,
        )
        assert _is_checklist_item_due(item, monday) is True

    def test_naive_last_triggered_at(self) -> None:
        """Timezone-naive last_triggered_at (from SQLite) should not raise TypeError."""
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        # SQLite returns naive datetimes -- simulate that here
        naive_last = datetime.datetime(2025, 6, 14, 8, 0)
        item = HeartbeatChecklistItem(
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
        item = HeartbeatChecklistItem(
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
        item = HeartbeatChecklistItem(
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
        db: Session,
        contractor: Contractor,
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
        action = await evaluate_heartbeat_need(db, contractor, ["Stale draft estimate"])
        assert action.action_type == "no_action"
        assert action.message == ""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_llm_says_send(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        db: Session,
        contractor: Contractor,
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
        action = await evaluate_heartbeat_need(db, contractor, ["Stale draft estimate"])
        assert action.action_type == "send_message"
        assert "draft estimate" in action.message

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_uses_heartbeat_model_when_set(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        db: Session,
        contractor: Contractor,
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
        await evaluate_heartbeat_need(db, contractor, ["test flag"])

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
        db: Session,
        contractor: Contractor,
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
        await evaluate_heartbeat_need(db, contractor, ["test flag"])
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
        db: Session,
        contractor: Contractor,
    ) -> None:
        """If LLM returns text instead of tool call, default to no_action."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        mock_settings.llm_max_tokens_heartbeat = 256
        mock_llm.return_value = make_text_response("I'm not sure what to do {broken json")
        action = await evaluate_heartbeat_need(db, contractor, ["test flag"])
        assert action.action_type == "no_action"
        assert action.priority == 0

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.amessages")
    async def test_passes_tools_to_acompletion(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        db: Session,
        contractor: Contractor,
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
        await evaluate_heartbeat_need(db, contractor, ["test flag"])
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
        db: Session,
        contractor: Contractor,
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
        await evaluate_heartbeat_need(db, contractor, ["test flag"])
        call_args = mock_llm.call_args
        system_content = call_args.kwargs["system"]
        assert "Respond with ONLY a JSON object" not in system_content
        assert "compose_message" in system_content


# ---------------------------------------------------------------------------
# run_heartbeat_for_contractor
# ---------------------------------------------------------------------------


class TestRunHeartbeatForContractor:
    @pytest.mark.asyncio
    async def test_skip_not_onboarded(self, db: Session, mock_messaging: MagicMock) -> None:
        c = Contractor(user_id="hb-new", phone="+15550000000", onboarding_complete=False)
        db.add(c)
        db.commit()
        result = await run_heartbeat_for_contractor(db, c, mock_messaging, 5)
        assert result is None
        mock_messaging.send_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=False)
    async def test_skip_outside_hours(
        self,
        _mock_hours: MagicMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_skip_rate_limited(
        self,
        _mock_hours: MagicMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
        # Add 5 heartbeat log entries to hit the daily limit
        for _ in range(5):
            db.add(HeartbeatLog(contractor_id=contractor.id))
        db.commit()
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)
        assert result is None

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_no_action_when_checks_clean(
        self,
        _mock_hours: MagicMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
        """When cheap checks return no flags, LLM is skipped and no message sent."""
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)
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
        mock_checks: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
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
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is not None
        assert result.action_type == "send_message"
        mock_messaging.send_text.assert_awaited_once_with(
            to=contractor.phone, body="Reminder: draft estimate pending!"
        )
        # Message should be recorded in DB
        msgs = db.query(Message).filter(Message.direction == "outbound").all()
        assert len(msgs) == 1
        assert msgs[0].body == "Reminder: draft estimate pending!"
        # Heartbeat log should be recorded for persistent rate limiting
        logs = db.query(HeartbeatLog).filter(HeartbeatLog.contractor_id == contractor.id).all()
        assert len(logs) == 1
        # LLM was called with the flags
        mock_eval.assert_awaited_once_with(
            db, contractor, ["Stale draft estimate"], messaging_service=mock_messaging
        )

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_sms_failure_graceful(
        self,
        _mock_hours: MagicMock,
        mock_checks: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
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
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)
        # Should still return the action, just not record a message
        assert result is not None
        assert result.action_type == "send_message"
        # No message recorded because SMS failed before recording
        msgs = db.query(Message).filter(Message.direction == "outbound").all()
        assert len(msgs) == 0

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_checklist_items_marked_triggered(
        self,
        _mock_hours: MagicMock,
        mock_checks: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
        """After sending a message, due checklist items should be marked as triggered."""
        item = HeartbeatChecklistItem(
            contractor_id=contractor.id,
            description="Check inbox",
            schedule="daily",
        )
        db.add(item)
        db.commit()
        db.refresh(item)

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
        await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        db.refresh(item)
        assert item.last_triggered_at is not None
        assert item.status == "active"  # daily stays active

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_once_item_completed_after_trigger(
        self,
        _mock_hours: MagicMock,
        mock_checks: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
        """A once-scheduled checklist item should be marked completed after triggering."""
        item = HeartbeatChecklistItem(
            contractor_id=contractor.id,
            description="Remind about meeting",
            schedule="once",
        )
        db.add(item)
        db.commit()
        db.refresh(item)

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
        await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        db.refresh(item)
        assert item.last_triggered_at is not None
        assert item.status == "completed"


# ---------------------------------------------------------------------------
# get_daily_heartbeat_count (persistent rate limiting)
# ---------------------------------------------------------------------------


class TestGetDailyHeartbeatCount:
    def test_zero_when_no_logs(self, db: Session, contractor: Contractor) -> None:
        assert get_daily_heartbeat_count(db, contractor.id) == 0

    def test_counts_today_only(self, db: Session, contractor: Contractor) -> None:
        """Logs from yesterday should not count toward today's limit."""
        # Add a log from today
        db.add(HeartbeatLog(contractor_id=contractor.id))
        db.commit()

        # Add a log from yesterday by manually setting created_at
        yesterday = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        old_log = HeartbeatLog(contractor_id=contractor.id, created_at=yesterday)
        db.add(old_log)
        db.commit()

        assert get_daily_heartbeat_count(db, contractor.id) == 1

    def test_counts_multiple_today(self, db: Session, contractor: Contractor) -> None:
        for _ in range(3):
            db.add(HeartbeatLog(contractor_id=contractor.id))
        db.commit()
        assert get_daily_heartbeat_count(db, contractor.id) == 3

    def test_scoped_to_contractor(self, db: Session, contractor: Contractor) -> None:
        """Logs from other contractors should not count."""
        other = Contractor(
            user_id="hb-other",
            phone="+15551112222",
            onboarding_complete=True,
        )
        db.add(other)
        db.commit()

        db.add(HeartbeatLog(contractor_id=other.id))
        db.commit()

        assert get_daily_heartbeat_count(db, contractor.id) == 0
        assert get_daily_heartbeat_count(db, other.id) == 1


# ---------------------------------------------------------------------------
# build_heartbeat_context
# ---------------------------------------------------------------------------


class TestBuildHeartbeatContext:
    @pytest.mark.asyncio
    async def test_includes_profile_and_flags(self, db: Session, contractor: Contractor) -> None:
        # Add a conversation so context builder works
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        msg = Message(conversation_id=conv.id, direction="inbound", body="I need a quote")
        db.add(msg)
        db.commit()

        flags = ["Stale draft estimate: Kitchen remodel"]
        prompt = await build_heartbeat_context(db, contractor, flags)

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
    @patch("backend.app.agent.heartbeat.SessionLocal")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_queries_onboarded(
        self, mock_messaging_cls: MagicMock, mock_session_local: MagicMock
    ) -> None:
        """Tick should query only onboarded contractors and close the listing session."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_session_local.return_value = mock_db

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_db.query.assert_called_once()
        mock_db.expunge_all.assert_called_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.SessionLocal")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_concurrent_processing(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_session_local: MagicMock,
        mock_run: AsyncMock,
    ) -> None:
        """tick() should process multiple contractors concurrently with per-contractor sessions."""
        mock_settings.heartbeat_concurrency = 2
        mock_settings.heartbeat_max_daily_messages = 5

        # Create mock contractors
        contractors = []
        for i in range(4):
            c = MagicMock()
            c.id = i + 1
            c.onboarding_complete = True
            contractors.append(c)

        # The first SessionLocal call is the listing session;
        # subsequent calls are per-contractor sessions.
        listing_db = MagicMock()
        listing_db.query.return_value.filter.return_value.all.return_value = contractors
        per_contractor_dbs = [MagicMock() for _ in contractors]
        mock_session_local.side_effect = [listing_db, *per_contractor_dbs]

        mock_run.return_value = None

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # Listing session should be closed after fetching contractors
        listing_db.expunge_all.assert_called_once()
        listing_db.close.assert_called_once()

        # run_heartbeat_for_contractor called once per contractor
        assert mock_run.await_count == len(contractors)

        # Each per-contractor session should be closed
        for db_mock in per_contractor_dbs:
            db_mock.close.assert_called_once()

        # Verify each call used its own DB session (not the listing session)
        for call_args in mock_run.call_args_list:
            used_db = call_args.kwargs.get("db") or call_args[0][0]
            assert used_db is not listing_db

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.SessionLocal")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_error_isolation(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_session_local: MagicMock,
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
            contractors.append(c)

        listing_db = MagicMock()
        listing_db.query.return_value.filter.return_value.all.return_value = contractors
        per_contractor_dbs = [MagicMock() for _ in contractors]
        mock_session_local.side_effect = [listing_db, *per_contractor_dbs]

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

        # All per-contractor sessions closed, even the failing one
        for db_mock in per_contractor_dbs:
            db_mock.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.run_heartbeat_for_contractor")
    @patch("backend.app.agent.heartbeat.SessionLocal")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    @patch("backend.app.agent.heartbeat.settings")
    async def test_tick_semaphore_limits_concurrency(
        self,
        mock_settings: MagicMock,
        mock_messaging_cls: MagicMock,
        mock_session_local: MagicMock,
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
            contractors.append(c)

        listing_db = MagicMock()
        listing_db.query.return_value.filter.return_value.all.return_value = contractors
        per_contractor_dbs = [MagicMock() for _ in contractors]
        mock_session_local.side_effect = [listing_db, *per_contractor_dbs]

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
    @patch("backend.app.agent.heartbeat.SessionLocal")
    @patch("backend.app.agent.heartbeat.get_default_channel")
    async def test_tick_no_contractors(
        self, mock_messaging_cls: MagicMock, mock_session_local: MagicMock
    ) -> None:
        """tick() with no onboarded contractors should return early."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_session_local.return_value = mock_db

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        # Listing session should still be closed
        mock_db.close.assert_called_once()
        # SessionLocal called only once (listing session, no per-contractor sessions)
        mock_session_local.assert_called_once()


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
        assert parse_frequency_to_minutes("abc") is None


# ---------------------------------------------------------------------------
# Heartbeat opt-in / frequency gate tests
# ---------------------------------------------------------------------------


class TestHeartbeatOptIn:
    @pytest.mark.asyncio
    async def test_opted_out_contractor_is_skipped(
        self, db: Session, contractor: Contractor
    ) -> None:
        """A contractor with heartbeat_opt_in=False should be skipped entirely."""
        contractor.heartbeat_opt_in = False
        db.commit()

        mock_messaging = AsyncMock()
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is None
        mock_messaging.send_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    async def test_opted_in_contractor_proceeds(
        self,
        mock_checks: MagicMock,
        mock_eval: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """A contractor with heartbeat_opt_in=True (default) should proceed normally."""
        assert contractor.heartbeat_opt_in is True

        mock_checks.return_value = CheapCheckResult(flags=[])
        mock_messaging = AsyncMock()

        with patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True):
            result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        # Should get no_action because cheap checks are clean, but NOT None (not skipped)
        assert result is not None
        assert result.action_type == "no_action"

    @pytest.mark.asyncio
    async def test_default_opt_in_is_true(self, db: Session) -> None:
        """New contractors should have heartbeat_opt_in=True by default."""
        c = Contractor(
            user_id="hb-opt-in-default",
            name="Default Contractor",
            onboarding_complete=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)

        assert c.heartbeat_opt_in is True

    @pytest.mark.asyncio
    async def test_default_frequency_is_empty(self, db: Session) -> None:
        """New contractors should have empty heartbeat_frequency by default."""
        c = Contractor(
            user_id="hb-freq-default",
            name="Default Freq Contractor",
            onboarding_complete=True,
        )
        db.add(c)
        db.commit()
        db.refresh(c)

        assert c.heartbeat_frequency == ""


class TestHeartbeatFrequencyGate:
    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    async def test_frequency_gate_blocks_when_too_soon(
        self,
        mock_checks: MagicMock,
        mock_biz_hours: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """If the contractor's frequency has not elapsed, skip the heartbeat."""
        contractor.heartbeat_frequency = "2h"
        db.commit()

        # Create a recent outbound message (30 minutes ago)
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        recent_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        msg = Message(
            conversation_id=conv.id,
            direction="outbound",
            body="Previous heartbeat",
            created_at=recent_time,
        )
        db.add(msg)
        db.commit()

        mock_messaging = AsyncMock()
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is None
        mock_checks.assert_not_called()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    async def test_frequency_gate_allows_when_elapsed(
        self,
        mock_checks: MagicMock,
        mock_biz_hours: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """If enough time has elapsed since the last message, proceed."""
        contractor.heartbeat_frequency = "1h"
        db.commit()

        # Create an old outbound message (2 hours ago)
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        old_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
        msg = Message(
            conversation_id=conv.id,
            direction="outbound",
            body="Old heartbeat",
            created_at=old_time,
        )
        db.add(msg)
        db.commit()

        mock_checks.return_value = CheapCheckResult(flags=[])
        mock_messaging = AsyncMock()

        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is not None
        assert result.action_type == "no_action"
        mock_checks.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    async def test_empty_frequency_skips_gate(
        self,
        mock_checks: MagicMock,
        mock_biz_hours: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """An empty heartbeat_frequency should not apply any frequency gate."""
        contractor.heartbeat_frequency = ""
        db.commit()

        mock_checks.return_value = CheapCheckResult(flags=[])
        mock_messaging = AsyncMock()

        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is not None
        mock_checks.assert_called_once()

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    @patch("backend.app.agent.heartbeat.run_cheap_checks")
    async def test_frequency_gate_allows_when_no_prior_messages(
        self,
        mock_checks: MagicMock,
        mock_biz_hours: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """If there are no prior outbound messages, the frequency gate should not block."""
        contractor.heartbeat_frequency = "1h"
        db.commit()

        mock_checks.return_value = CheapCheckResult(flags=[])
        mock_messaging = AsyncMock()

        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, 5)

        assert result is not None
        mock_checks.assert_called_once()
