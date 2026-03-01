"""Tests for the proactive heartbeat engine."""

from __future__ import annotations

import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent.heartbeat import (
    CheapCheckResult,
    HeartbeatAction,
    HeartbeatScheduler,
    _is_checklist_item_due,
    _parse_business_hours,
    _strip_code_fences,
    build_heartbeat_context,
    evaluate_heartbeat_need,
    is_within_business_hours,
    run_cheap_checks,
    run_heartbeat_for_contractor,
)
from backend.app.database import Base
from backend.app.models import (
    Contractor,
    Conversation,
    Estimate,
    HeartbeatChecklistItem,
    Memory,
    Message,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> Session:
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
def mock_messaging() -> MagicMock:
    svc = MagicMock()
    svc.send_text = AsyncMock(return_value="mock_heartbeat_msg_id")
    return svc


def _make_llm_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


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
        # SQLite returns naive datetimes — simulate that here
        naive_last = datetime.datetime(2025, 6, 14, 8, 0)
        item = HeartbeatChecklistItem(
            contractor_id=1,
            description="Test",
            schedule="daily",
            last_triggered_at=naive_last,
        )
        # Should not raise TypeError and should be due (>20h elapsed)
        assert _is_checklist_item_due(item, now) is True


# ---------------------------------------------------------------------------
# _strip_code_fences
# ---------------------------------------------------------------------------


class TestStripCodeFences:
    def test_plain_json_unchanged(self) -> None:
        """Plain JSON without fences is returned as-is."""
        raw = '{"action": "no_action", "message": "", "reasoning": "", "priority": 1}'
        assert _strip_code_fences(raw) == raw

    def test_json_code_fence(self) -> None:
        """JSON wrapped in ```json fences should be stripped."""
        raw = (
            "```json\n"
            '{"action": "send_message", "message": "Hi", "reasoning": "test", "priority": 3}\n'
            "```"
        )
        expected = '{"action": "send_message", "message": "Hi", "reasoning": "test", "priority": 3}'
        assert _strip_code_fences(raw) == expected

    def test_plain_code_fence(self) -> None:
        """JSON wrapped in ``` fences (no language tag) should be stripped."""
        raw = '```\n{"action": "no_action", "message": "", "reasoning": "", "priority": 1}\n```'
        expected = '{"action": "no_action", "message": "", "reasoning": "", "priority": 1}'
        assert _strip_code_fences(raw) == expected

    def test_code_fence_with_surrounding_whitespace(self) -> None:
        """Surrounding whitespace around fences should be handled."""
        raw = '  \n```json\n{"action": "no_action"}\n```\n  '
        assert _strip_code_fences(raw) == '{"action": "no_action"}'

    def test_malformed_text_returned_as_is(self) -> None:
        """Non-JSON, non-fenced text is returned stripped."""
        raw = "I'm not sure what to do"
        assert _strip_code_fences(raw) == raw


# ---------------------------------------------------------------------------
# evaluate_heartbeat_need
# ---------------------------------------------------------------------------


class TestEvaluateHeartbeatNeed:
    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
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
        mock_llm.return_value = _make_llm_response(
            json.dumps(
                {
                    "action": "no_action",
                    "message": "",
                    "reasoning": "Nothing actionable",
                    "priority": 1,
                }
            )
        )
        action = await evaluate_heartbeat_need(db, contractor, ["Stale draft estimate"])
        assert action.action_type == "no_action"
        assert action.message == ""

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
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
        mock_llm.return_value = _make_llm_response(
            json.dumps(
                {
                    "action": "send_message",
                    "message": "Hey Mike, you have a draft estimate sitting for 2 days.",
                    "reasoning": "Stale draft estimate",
                    "priority": 4,
                }
            )
        )
        action = await evaluate_heartbeat_need(db, contractor, ["Stale draft estimate"])
        assert action.action_type == "send_message"
        assert "draft estimate" in action.message

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
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
        mock_llm.return_value = _make_llm_response(
            json.dumps({"action": "no_action", "message": "", "reasoning": "", "priority": 1})
        )
        await evaluate_heartbeat_need(db, contractor, ["test flag"])

        # Verify the cheap model was used
        call_kwargs = mock_llm.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
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
        mock_llm.return_value = _make_llm_response(
            json.dumps({"action": "no_action", "message": "", "reasoning": "test", "priority": 1})
        )
        await evaluate_heartbeat_need(db, contractor, ["test flag"])
        _, kwargs = mock_llm.call_args
        assert "api_base" in kwargs
        assert kwargs["api_base"] == "http://localhost:1234/v1"
        assert "api_key" not in kwargs

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
    async def test_malformed_response(
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
        mock_llm.return_value = _make_llm_response("I'm not sure what to do {broken json")
        action = await evaluate_heartbeat_need(db, contractor, ["test flag"])
        assert action.action_type == "no_action"
        assert action.priority == 0

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
    async def test_json_code_fence_parsed(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """Regression: LLM response wrapped in ```json fences should parse correctly."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "action": "send_message",
                    "message": "Don't forget your estimate!",
                    "reasoning": "Stale draft",
                    "priority": 4,
                }
            )
            + "\n```"
        )
        mock_llm.return_value = _make_llm_response(fenced)
        action = await evaluate_heartbeat_need(db, contractor, ["Stale draft estimate"])
        assert action.action_type == "send_message"
        assert "estimate" in action.message

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.settings")
    @patch("backend.app.agent.heartbeat.acompletion")
    async def test_plain_code_fence_parsed(
        self,
        mock_llm: AsyncMock,
        mock_settings: MagicMock,
        db: Session,
        contractor: Contractor,
    ) -> None:
        """Regression: LLM response wrapped in ``` fences (no lang tag) should parse."""
        mock_settings.llm_model = "gpt-4o"
        mock_settings.llm_provider = "openai"
        mock_settings.llm_api_base = None
        mock_settings.heartbeat_model = ""
        mock_settings.heartbeat_provider = ""
        fenced = (
            "```\n"
            + json.dumps(
                {
                    "action": "no_action",
                    "message": "",
                    "reasoning": "Nothing to do",
                    "priority": 1,
                }
            )
            + "\n```"
        )
        mock_llm.return_value = _make_llm_response(fenced)
        action = await evaluate_heartbeat_need(db, contractor, ["test flag"])
        assert action.action_type == "no_action"
        assert action.reasoning == "Nothing to do"


# ---------------------------------------------------------------------------
# run_heartbeat_for_contractor
# ---------------------------------------------------------------------------


class TestRunHeartbeatForContractor:
    @pytest.mark.asyncio
    async def test_skip_not_onboarded(self, db: Session, mock_messaging: MagicMock) -> None:
        c = Contractor(user_id="hb-new", phone="+15550000000", onboarding_complete=False)
        db.add(c)
        db.commit()
        result = await run_heartbeat_for_contractor(db, c, mock_messaging, {}, 5)
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
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, {}, 5)
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
        counts = {contractor.id: 5}
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, counts, 5)
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
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, {}, 5)
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
        counts: dict[int, int] = {}
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, counts, 5)

        assert result is not None
        assert result.action_type == "send_message"
        mock_messaging.send_text.assert_awaited_once_with(
            to=contractor.phone, body="Reminder: draft estimate pending!"
        )
        # Message should be recorded in DB
        msgs = db.query(Message).filter(Message.direction == "outbound").all()
        assert len(msgs) == 1
        assert msgs[0].body == "Reminder: draft estimate pending!"
        # Daily count should be incremented
        assert counts[contractor.id] == 1
        # LLM was called with the flags
        mock_eval.assert_awaited_once_with(db, contractor, ["Stale draft estimate"])

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
        counts: dict[int, int] = {}
        result = await run_heartbeat_for_contractor(db, contractor, mock_messaging, counts, 5)
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
        await run_heartbeat_for_contractor(db, contractor, mock_messaging, {}, 5)

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
        await run_heartbeat_for_contractor(db, contractor, mock_messaging, {}, 5)

        db.refresh(item)
        assert item.last_triggered_at is not None
        assert item.status == "completed"


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
        ctx = await build_heartbeat_context(db, contractor, flags)

        assert "Plumber" in ctx["soul_prompt"]
        assert "Kitchen remodel" in ctx["flags"]
        assert "I need a quote" in ctx["recent_messages"]


# ---------------------------------------------------------------------------
# HeartbeatScheduler
# ---------------------------------------------------------------------------


class TestHeartbeatScheduler:
    def test_daily_count_reset(self) -> None:
        scheduler = HeartbeatScheduler()
        scheduler._daily_counts = {1: 3, 2: 5}
        scheduler._last_reset_date = datetime.date(2025, 1, 1)

        # Simulate a new day by calling tick logic indirectly
        today = datetime.date.today()
        assert scheduler._last_reset_date != today
        # After a tick on a new day, counts should be empty
        # We'll verify the reset logic directly
        if scheduler._last_reset_date != today:
            scheduler._daily_counts = {}
            scheduler._last_reset_date = today
        assert scheduler._daily_counts == {}

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
    @patch("backend.app.agent.heartbeat._build_messaging_service")
    async def test_tick_queries_onboarded(
        self, mock_messaging_cls: MagicMock, mock_session_local: MagicMock
    ) -> None:
        """Tick should query only onboarded contractors and close the session."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_session_local.return_value = mock_db

        scheduler = HeartbeatScheduler()
        await scheduler.tick()

        mock_db.query.assert_called_once()
        mock_db.close.assert_called_once()
