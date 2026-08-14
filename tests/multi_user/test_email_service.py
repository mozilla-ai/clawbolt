"""Tests for the SMTP email service and waitlist approval email send."""

from __future__ import annotations

import smtplib
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AdminAuditLog, AllowedEmail, Subscription, WaitlistEntry
from backend.app.services import email_service


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


@pytest.fixture
def smtp_configured() -> object:
    """Patch settings to look as if SES SMTP is configured."""
    with (
        patch.object(email_service.settings, "smtp_host", "smtp.example.com"),
        patch.object(email_service.settings, "smtp_port", 587),
        patch.object(email_service.settings, "smtp_username", "AKIA_FAKE"),
        patch.object(email_service.settings, "smtp_password", "secret"),
        patch.object(email_service.settings, "smtp_from_email", "hello@example.com"),
        patch.object(email_service.settings, "app_base_url", "https://app.example.com"),
    ):
        yield


class TestEmailServiceUnit:
    """Direct unit tests for email_service.send_waitlist_approved."""

    async def test_no_op_when_smtp_unset(self) -> None:
        """When SMTP_HOST is empty, send returns False without touching SMTP."""
        with (
            patch.object(email_service.settings, "smtp_host", ""),
            patch.object(email_service.settings, "smtp_from_email", ""),
            patch.object(email_service, "smtplib") as mock_smtplib,
        ):
            ok = await email_service.send_waitlist_approved("user@example.com")

        assert ok is False
        mock_smtplib.SMTP.assert_not_called()

    async def test_send_calls_smtp_with_starttls_and_login(self, smtp_configured: None) -> None:
        """Configured send: STARTTLS, login, send_message all invoked."""
        smtp_instance = MagicMock()

        with patch.object(
            email_service.smtplib, "SMTP", return_value=smtp_instance
        ) as mock_smtp_cls:
            ok = await email_service.send_waitlist_approved("user@example.com")

        assert ok is True
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("AKIA_FAKE", "secret")
        smtp_instance.send_message.assert_called_once()

        sent_msg = smtp_instance.send_message.call_args.args[0]
        assert sent_msg["From"] == "hello@example.com"
        assert sent_msg["To"] == "user@example.com"
        assert "clawbolt" in sent_msg["Subject"].lower()
        # Multipart with text + html
        body_text = sent_msg.get_body(preferencelist=("plain",)).get_content()
        body_html = sent_msg.get_body(preferencelist=("html",)).get_content()
        assert "https://app.example.com/app/login" in body_text
        assert "https://app.example.com/app/login" in body_html

    async def test_send_skips_login_when_username_empty(self, smtp_configured: None) -> None:
        """Anonymous SMTP relays (no auth): login is skipped, send still happens."""
        smtp_instance = MagicMock()

        with (
            patch.object(email_service.settings, "smtp_username", ""),
            patch.object(email_service.smtplib, "SMTP", return_value=smtp_instance),
        ):
            ok = await email_service.send_waitlist_approved("user@example.com")

        assert ok is True
        smtp_instance.login.assert_not_called()
        smtp_instance.send_message.assert_called_once()

    async def test_send_failure_returns_false(self, smtp_configured: None) -> None:
        """SMTP errors are swallowed; the function returns False."""
        with patch.object(
            email_service.smtplib,
            "SMTP",
            side_effect=smtplib.SMTPException("transient SES failure"),
        ):
            ok = await email_service.send_waitlist_approved("user@example.com")

        assert ok is False

    async def test_send_greets_by_first_name(self, smtp_configured: None) -> None:
        """A real name yields a personalized greeting in both text and HTML."""
        smtp_instance = MagicMock()

        with patch.object(email_service.smtplib, "SMTP", return_value=smtp_instance):
            ok = await email_service.send_waitlist_approved("user@example.com", "Alice Doe")

        assert ok is True
        sent_msg = smtp_instance.send_message.call_args.args[0]
        text_body = sent_msg.get_body(preferencelist=("plain",)).get_content()
        html_body = sent_msg.get_body(preferencelist=("html",)).get_content()
        assert "Hi Alice," in text_body
        assert "Hi Alice," in html_body

    async def test_send_falls_back_for_legacy_name(self, smtp_configured: None) -> None:
        """Legacy rows backfilled with ``user`` greet as ``there``, not ``Hi user``."""
        smtp_instance = MagicMock()

        with patch.object(email_service.smtplib, "SMTP", return_value=smtp_instance):
            await email_service.send_waitlist_approved("user@example.com", "user")

        sent_msg = smtp_instance.send_message.call_args.args[0]
        text_body = sent_msg.get_body(preferencelist=("plain",)).get_content()
        assert "Hi there," in text_body
        assert "Hi user," not in text_body

    async def test_send_escapes_html_in_name(self, smtp_configured: None) -> None:
        """Names with HTML metacharacters are escaped before rendering."""
        smtp_instance = MagicMock()

        with patch.object(email_service.smtplib, "SMTP", return_value=smtp_instance):
            await email_service.send_waitlist_approved("user@example.com", "<script>")

        sent_msg = smtp_instance.send_message.call_args.args[0]
        html_body = sent_msg.get_body(preferencelist=("html",)).get_content()
        assert "<script>" not in html_body
        assert "&lt;script&gt;" in html_body


class TestTransportReporting:
    """A failed send has to explain itself, and it has to fail promptly.

    Both were regressions from the same incident: a test alert from the admin
    tab held the request for 45s and then reported only "Not sent". The cause
    was a dropped TCP connection, which nothing in the response or the log line
    distinguished from a typo'd password.
    """

    @pytest.fixture(autouse=True)
    def _reset(self) -> None:
        email_service.reset_for_tests()

    async def test_timeout_explanation_names_the_endpoint_and_the_likely_cause(
        self, smtp_configured: None
    ) -> None:
        with patch.object(email_service, "_send_sync", side_effect=TimeoutError("timed out")):
            ok = await email_service.send_waitlist_approved("user@example.com")

        assert ok is False
        status = email_service.transport_status()
        assert "smtp.example.com:587" in status.last_error
        assert "blocked" in status.last_error
        assert status.last_success_at is None
        assert status.last_error_at is not None

    async def test_auth_failure_is_not_reported_as_a_network_problem(
        self, smtp_configured: None
    ) -> None:
        with patch.object(
            email_service,
            "_send_sync",
            side_effect=smtplib.SMTPAuthenticationError(535, b"Authentication Credentials Invalid"),
        ):
            await email_service.send_waitlist_approved("user@example.com")

        error = email_service.transport_status().last_error
        assert "rejected the credentials" in error
        assert "blocked" not in error

    async def test_send_is_bounded_by_twice_the_socket_timeout(self, smtp_configured: None) -> None:
        """``socket.create_connection`` retries every resolved address.

        At the old 15s socket timeout a three-A-record host (SES is one) held
        the caller for 45s. The overall budget is what keeps a blocked port from
        pinning an admin request open for a minute.
        """

        def _hang(_msg: object) -> None:
            time.sleep(10)

        started = time.monotonic()
        with (
            patch.object(email_service.settings, "smtp_timeout_seconds", 1),
            patch.object(email_service, "_send_sync", side_effect=_hang),
        ):
            ok = await email_service.send_waitlist_approved("user@example.com")
        elapsed = time.monotonic() - started

        assert ok is False
        assert elapsed < 5
        assert "Timed out" in email_service.transport_status().last_error

    async def test_success_clears_the_previous_error(self, smtp_configured: None) -> None:
        with patch.object(email_service, "_send_sync", side_effect=TimeoutError("timed out")):
            await email_service.send_waitlist_approved("user@example.com")
        with patch.object(email_service.smtplib, "SMTP", return_value=MagicMock()):
            assert await email_service.send_waitlist_approved("user@example.com") is True

        status = email_service.transport_status()
        assert status.last_error == ""
        assert status.last_success_at is not None


class TestDeliveryDiagnostics:
    """The diagnostic exists to answer one question: is anything getting out?"""

    @staticmethod
    def _ports(reachable: set[int]) -> object:
        async def _check(_host: str, port: int) -> email_service.PortCheck:
            return email_service.PortCheck(
                port=port,
                reachable=port in reachable,
                detail="connected in 4ms" if port in reachable else "no response within 5s",
            )

        return _check

    async def test_unconfigured_makes_no_connections(self) -> None:
        with (
            patch.object(email_service.settings, "smtp_host", ""),
            patch.object(email_service.settings, "smtp_from_email", ""),
            patch.object(email_service, "_check_port") as check,
        ):
            result = await email_service.diagnose_transport()
        assert result["configured"] is False
        assert "not configured" in str(result["conclusion"])
        check.assert_not_called()

    async def test_a_working_handshake_points_downstream(self, smtp_configured: None) -> None:
        with (
            patch.object(email_service, "_check_port", new=self._ports({587})),
            patch.object(email_service, "_handshake_sync", return_value="EHLO ok"),
        ):
            result = await email_service.diagnose_transport()
        assert result["handshake_ok"] is True
        # The transport is fine, so the operator is told where to look instead.
        assert "downstream of the transport" in str(result["conclusion"])

    async def test_nothing_reachable_reads_as_a_platform_block(self, smtp_configured: None) -> None:
        with (
            patch.object(email_service, "_check_port", new=self._ports(set())),
            patch.object(email_service, "_handshake_sync", side_effect=TimeoutError("timed out")),
        ):
            result = await email_service.diagnose_transport()
        conclusion = str(result["conclusion"])
        assert "network-level block" in conclusion
        assert "HTTPS email API" in conclusion

    async def test_a_reachable_alternate_port_is_recommended(self, smtp_configured: None) -> None:
        # SES publishes 2587 precisely because networks block 587. Naming the
        # reachable port turns the diagnostic into a one-line fix.
        with (
            patch.object(email_service, "_check_port", new=self._ports({2587})),
            patch.object(email_service, "_handshake_sync", side_effect=TimeoutError("timed out")),
        ):
            result = await email_service.diagnose_transport()
        conclusion = str(result["conclusion"])
        assert "Port 587 is blocked" in conclusion
        assert "2587" in conclusion

    async def test_a_reachable_port_with_a_refused_session_blames_the_session(
        self, smtp_configured: None
    ) -> None:
        with (
            patch.object(email_service, "_check_port", new=self._ports({587})),
            patch.object(
                email_service,
                "_handshake_sync",
                side_effect=smtplib.SMTPAuthenticationError(535, b"nope"),
            ),
        ):
            result = await email_service.diagnose_transport()
        assert "the failure is in the SMTP session itself" in str(result["conclusion"])


class TestApproveSendsEmail:
    """Approval endpoint integrates with the email service."""

    def test_approve_sends_email_when_configured(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
        smtp_configured: None,
    ) -> None:
        client.post(
            "/api/waitlist/join",
            json={
                "email": "ready@example.com",
                "name": "Charlie",
                "use_case": "Electrician in San Diego.",
            },
        )
        entry = db_session.query(WaitlistEntry).filter_by(email="ready@example.com").first()
        assert entry is not None

        smtp_instance = MagicMock()

        with patch.object(email_service.smtplib, "SMTP", return_value=smtp_instance):
            resp = client.post(f"/api/admin/waitlist/{entry.id}/approve")

        assert resp.status_code == 200
        smtp_instance.send_message.assert_called_once()
        sent_msg = smtp_instance.send_message.call_args.args[0]
        assert sent_msg["To"] == "ready@example.com"
        text_body = sent_msg.get_body(preferencelist=("plain",)).get_content()
        assert "Hi Charlie," in text_body

        # Approval still landed in allowed_emails
        allowed = db_session.query(AllowedEmail).filter_by(email="ready@example.com").first()
        assert allowed is not None

        # Audit log records the full signup context plus the email outcome.
        # The waitlist row is gone after approve, so the audit log is the
        # only place an operator can recover what the user wrote.
        audit = (
            db_session.query(AdminAuditLog)
            .filter_by(action="approve_waitlist")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert audit.detail is not None
        assert audit.detail.get("approval_email_sent") is True
        assert audit.detail.get("email") == "ready@example.com"
        assert audit.detail.get("name") == "Charlie"
        assert audit.detail.get("use_case") == "Electrician in San Diego."

    def test_approve_succeeds_when_smtp_unconfigured(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Default: SMTP_HOST="" -> approve still returns 200, no SMTP call."""
        client.post("/api/waitlist/join", json={"email": "noemail@example.com"})
        entry = db_session.query(WaitlistEntry).filter_by(email="noemail@example.com").first()
        assert entry is not None

        with patch.object(email_service.smtplib, "SMTP") as mock_smtp_cls:
            resp = client.post(f"/api/admin/waitlist/{entry.id}/approve")

        assert resp.status_code == 200
        mock_smtp_cls.assert_not_called()

        allowed = db_session.query(AllowedEmail).filter_by(email="noemail@example.com").first()
        assert allowed is not None

    def test_approve_succeeds_when_smtp_send_fails(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
        smtp_configured: None,
    ) -> None:
        """Best-effort send: SES outage must not unwind the approval."""
        client.post("/api/waitlist/join", json={"email": "ses-down@example.com"})
        entry = db_session.query(WaitlistEntry).filter_by(email="ses-down@example.com").first()
        assert entry is not None

        with patch.object(
            email_service.smtplib,
            "SMTP",
            side_effect=smtplib.SMTPException("SES is having a moment"),
        ):
            resp = client.post(f"/api/admin/waitlist/{entry.id}/approve")

        assert resp.status_code == 200

        allowed = db_session.query(AllowedEmail).filter_by(email="ses-down@example.com").first()
        assert allowed is not None
        gone = db_session.query(WaitlistEntry).filter_by(email="ses-down@example.com").first()
        assert gone is None

        # Audit log captures the failed send so operators can resend manually
        audit = (
            db_session.query(AdminAuditLog)
            .filter_by(action="approve_waitlist")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert audit.detail is not None
        assert audit.detail.get("approval_email_sent") is False
