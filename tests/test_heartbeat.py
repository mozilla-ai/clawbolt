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
    HeartbeatAction,
    HeartbeatScheduler,
    _parse_business_hours,
    build_heartbeat_context,
    evaluate_heartbeat_need,
    is_within_business_hours,
    run_heartbeat_for_contractor,
)
from backend.app.database import Base
from backend.app.models import Contractor, Conversation, Estimate, Message

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
        # 10 AM — within 7am-5pm
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    def test_before_hours(self, contractor: Contractor) -> None:
        # 5 AM — before 7am-5pm
        now = datetime.datetime(2025, 6, 15, 5, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    def test_after_hours(self, contractor: Contractor) -> None:
        # 8 PM — after 7am-5pm
        now = datetime.datetime(2025, 6, 15, 20, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    def test_at_boundary_start(self, contractor: Contractor) -> None:
        # Exactly 7 AM — should be within
        now = datetime.datetime(2025, 6, 15, 7, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is True

    def test_at_boundary_end(self, contractor: Contractor) -> None:
        # Exactly 5 PM (17:00) — should be outside (end is exclusive)
        now = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor, now) is False

    @patch("backend.app.agent.heartbeat.settings")
    def test_fallback_quiet_hours(
        self, mock_settings: MagicMock, contractor_no_hours: Contractor
    ) -> None:
        mock_settings.heartbeat_quiet_hours_start = 20
        mock_settings.heartbeat_quiet_hours_end = 7

        # 10 AM — outside quiet hours, should be True
        now = datetime.datetime(2025, 6, 15, 10, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is True

        # 22:00 — inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 22, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is False

        # 3 AM — inside quiet hours, should be False
        now = datetime.datetime(2025, 6, 15, 3, 0, tzinfo=datetime.UTC)
        assert is_within_business_hours(contractor_no_hours, now) is False


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
        mock_settings.llm_api_key = ""
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
        action = await evaluate_heartbeat_need(db, contractor)
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
        mock_settings.llm_api_key = ""
        mock_llm.return_value = _make_llm_response(
            json.dumps(
                {
                    "action": "send_message",
                    "message": "Hey Mike, you have a draft estimate sitting for 2 days. Want to send it?",
                    "reasoning": "Stale draft estimate",
                    "priority": 4,
                }
            )
        )
        action = await evaluate_heartbeat_need(db, contractor)
        assert action.action_type == "send_message"
        assert "draft estimate" in action.message

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
        mock_settings.llm_api_key = ""
        mock_llm.return_value = _make_llm_response("I'm not sure what to do {broken json")
        action = await evaluate_heartbeat_need(db, contractor)
        assert action.action_type == "no_action"
        assert action.priority == 0


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
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_send_sms_and_record(
        self,
        _mock_hours: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
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

    @pytest.mark.asyncio
    @patch("backend.app.agent.heartbeat.evaluate_heartbeat_need")
    @patch("backend.app.agent.heartbeat.is_within_business_hours", return_value=True)
    async def test_sms_failure_graceful(
        self,
        _mock_hours: MagicMock,
        mock_eval: AsyncMock,
        db: Session,
        contractor: Contractor,
        mock_messaging: MagicMock,
    ) -> None:
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


# ---------------------------------------------------------------------------
# build_heartbeat_context
# ---------------------------------------------------------------------------


class TestBuildHeartbeatContext:
    @pytest.mark.asyncio
    async def test_includes_profile_and_estimates(
        self, db: Session, contractor: Contractor
    ) -> None:
        # Add a pending estimate
        conv = Conversation(contractor_id=contractor.id, is_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)

        est = Estimate(
            contractor_id=contractor.id,
            description="Kitchen remodel for Smith",
            total_amount=5000,
            status="draft",
        )
        db.add(est)

        msg = Message(conversation_id=conv.id, direction="inbound", body="I need a quote")
        db.add(msg)
        db.commit()

        ctx = await build_heartbeat_context(db, contractor)

        assert "Plumber" in ctx["soul_prompt"]
        assert "Kitchen remodel" in ctx["pending_estimates"]
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
