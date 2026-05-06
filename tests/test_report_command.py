"""Tests for the inline ``/report`` channel command (issue #325 item 5).

Covers:
- Parsing (``_parse_report_command``): exact match, with reason, false
  positives like ``/reportbot`` and ``please /report`` are not commands.
- End-to-end intercept inside ``process_inbound_from_bus``: a /report
  message persists a row, sends an ack, and does NOT continue to the
  agent pipeline.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.ingestion import (
    InboundMessage,
    _parse_report_command,
    process_inbound_from_bus,
)
from backend.app.bus import message_bus
from backend.app.models import ChatSession, Message, ReportedConversation, User
from tests.db_test_utils import open_test_db_session


class TestParseReportCommand:
    def test_bare_report_returns_empty_reason(self) -> None:
        assert _parse_report_command("/report") == ""

    def test_report_with_reason(self) -> None:
        assert _parse_report_command("/report bot was rude") == "bot was rude"

    def test_case_insensitive_command_word(self) -> None:
        """Recognize ``/Report``, ``/REPORT``, etc. as the same command.

        Mobile keyboards autocapitalize the first letter, so we should
        accept ``/Report`` from a user who didn't override the
        autocap.
        """
        assert _parse_report_command("/Report something happened") == "something happened"
        assert _parse_report_command("/REPORT") == ""

    def test_leading_and_trailing_whitespace_is_stripped(self) -> None:
        assert _parse_report_command("  /report   the bot lied   ") == "the bot lied"

    def test_reportbot_is_not_a_command(self) -> None:
        """``/reportbot`` (no space after the prefix) is a different word."""
        assert _parse_report_command("/reportbot is a tool") is None

    def test_mid_sentence_report_is_not_a_command(self) -> None:
        """The command must be at the start of the message.

        Otherwise a user typing "please /report this" mid-sentence in
        normal conversation would silently flag the conversation, which
        violates the principle of least surprise.
        """
        assert _parse_report_command("please /report this") is None
        assert _parse_report_command("about to /report something") is None

    def test_empty_input_is_not_a_command(self) -> None:
        assert _parse_report_command("") is None
        assert _parse_report_command("   ") is None

    def test_non_command_message_returns_none(self) -> None:
        assert _parse_report_command("hello there") is None
        assert _parse_report_command("/help") is None

    def test_reason_is_truncated_at_4kb(self) -> None:
        """A pasted-transcript-sized reason is silently truncated rather
        than rejected, so the report still files. Bounds row size for
        the admin UI's list view and the JSON envelope."""
        from backend.app.agent.ingestion import _REPORT_REASON_MAX_LEN

        huge = "a" * (_REPORT_REASON_MAX_LEN * 4)
        result = _parse_report_command(f"/report {huge}")
        assert result is not None
        assert len(result) == _REPORT_REASON_MAX_LEN
        assert result == "a" * _REPORT_REASON_MAX_LEN


class TestReportInterception:
    """End-to-end behavior: /report short-circuits the agent pipeline."""

    @pytest.fixture()
    def report_user(self) -> User:
        """Create a User that exists in the test database."""
        db = open_test_db_session()
        try:
            user = User(
                id=str(uuid.uuid4()),
                user_id=f"google_{uuid.uuid4().hex[:8]}",
                phone="",
                onboarding_complete=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            db.expunge(user)
            return user
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_report_persists_row_and_sends_ack(self, report_user: User) -> None:
        """A ``/report`` message: writes one ReportedConversation row,
        sends one ack to the bus, never calls the agent pipeline."""
        # Drain the bus so we only see this test's outbound.
        while not message_bus.outbound.empty():
            message_bus.outbound.get_nowait()

        inbound = InboundMessage(
            channel="telegram",
            sender_id=report_user.user_id,
            text="/report the bot said something rude",
        )

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=report_user,
            ),
            patch(
                "backend.app.agent.ingestion._check_channel_route_enabled",
                return_value=True,
            ),
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
        ):
            await process_inbound_from_bus(inbound)

        # Pipeline must NOT have been called for a /report.
        mock_handle.assert_not_called()

        # Exactly one ReportedConversation row exists for this user.
        db = open_test_db_session()
        try:
            rows = (
                db.query(ReportedConversation)
                .filter(ReportedConversation.user_id == report_user.id)
                .all()
            )
            assert len(rows) == 1
            assert rows[0].reason == "the bot said something rude"
            assert rows[0].dismissed_at is None
            assert rows[0].reviewed_admin_user_id is None
        finally:
            db.close()

        # One ack on the bus, with the canonical body.
        assert not message_bus.outbound.empty()
        ack = message_bus.outbound.get_nowait()
        assert ack.chat_id == report_user.user_id
        assert "flagged" in ack.content.lower()
        # Drain anything else (typing indicators) but the only real
        # outbound should be the ack.
        while not message_bus.outbound.empty():
            extra = message_bus.outbound.get_nowait()
            assert extra.is_typing_indicator, (
                f"Unexpected non-typing outbound after /report ack: {extra.content!r}"
            )

    @pytest.mark.asyncio
    async def test_report_anchor_seq_points_at_latest_message(self, report_user: User) -> None:
        """The ``anchor_seq`` column captures the seq of the last
        message in the session at report time so admins can highlight
        the surrounding window."""
        # Pre-seed a session with two prior messages so seq=2 is the
        # latest before /report fires.
        db = open_test_db_session()
        try:
            cs = ChatSession(
                session_id=f"sess-{uuid.uuid4().hex[:8]}",
                user_id=report_user.id,
                channel="telegram",
            )
            db.add(cs)
            db.flush()
            db.add_all(
                [
                    Message(session_id=cs.id, seq=1, direction="inbound", body="hi"),
                    Message(session_id=cs.id, seq=2, direction="outbound", body="hello!"),
                ]
            )
            db.commit()
            session_db_id = cs.id
        finally:
            db.close()

        # Drain the bus.
        while not message_bus.outbound.empty():
            message_bus.outbound.get_nowait()

        inbound = InboundMessage(
            channel="telegram",
            sender_id=report_user.user_id,
            text="/report",
        )

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=report_user,
            ),
            patch(
                "backend.app.agent.ingestion._check_channel_route_enabled",
                return_value=True,
            ),
        ):
            await process_inbound_from_bus(inbound)

        db = open_test_db_session()
        try:
            row = (
                db.query(ReportedConversation)
                .filter(ReportedConversation.user_id == report_user.id)
                .filter(ReportedConversation.session_id == session_db_id)
                .one()
            )
            assert row.anchor_seq == 2
            assert row.reason == ""
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_non_report_message_is_not_intercepted(self, report_user: User) -> None:
        """A normal inbound text must reach the agent pipeline (no
        ReportedConversation row, no canned ack, no short-circuit)."""
        while not message_bus.outbound.empty():
            message_bus.outbound.get_nowait()

        inbound = InboundMessage(
            channel="telegram",
            sender_id=report_user.user_id,
            text="hi there, how's it going",
        )

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=report_user,
            ),
            patch(
                "backend.app.agent.ingestion._check_channel_route_enabled",
                return_value=True,
            ),
            patch(
                "backend.app.agent.ingestion.get_approval_gate",
            ) as mock_gate,
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch(
                "backend.app.agent.ingestion.settings",
            ) as mock_settings,
        ):
            mock_gate.return_value.has_pending.return_value = False
            mock_settings.message_batch_window_ms = 0
            mock_settings.agent_processing_timeout_seconds = 60.0
            await process_inbound_from_bus(inbound)

        # Pipeline IS called for normal traffic.
        mock_handle.assert_called_once()

        # No ReportedConversation row was written.
        db = open_test_db_session()
        try:
            count = (
                db.query(ReportedConversation)
                .filter(ReportedConversation.user_id == report_user.id)
                .count()
            )
            assert count == 0
        finally:
            db.close()
