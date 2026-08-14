"""Tests for the admin monitoring endpoints and the operator email templates."""

from __future__ import annotations

import time
from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.auth.admin_dep import get_current_admin
from backend.app.models import User
from backend.app.services import admin_alerts, email_service, health_monitor
from backend.app.services.admin_alerts import AlertSummary
from backend.app.services.health_monitor import HealthTransition


@pytest.fixture
def admin_client(client: TestClient, test_user: User) -> Generator[TestClient]:
    """Reuse the standard client with the admin gate satisfied."""
    from tests.multi_user.conftest import MULTI_USER_APP as app

    app.dependency_overrides[get_current_admin] = lambda: test_user
    yield client
    app.dependency_overrides.pop(get_current_admin, None)


@pytest.fixture(autouse=True)
def _reset_monitor_state() -> Generator[None]:
    admin_alerts.reset_for_tests()
    health_monitor.health_monitor.reset_for_tests()
    email_service.reset_for_tests()
    yield
    admin_alerts.reset_for_tests()
    health_monitor.health_monitor.reset_for_tests()
    email_service.reset_for_tests()


def _await_run(admin_client: TestClient, attempts: int = 60) -> dict:
    """Poll monitoring status until the in-flight probe run finishes.

    The run endpoint deliberately does not await the run, so the assertions
    about its outcome have to wait for the background task the way the admin tab
    does. The app runs on its own event loop in a TestClient portal thread, so
    sleeping here lets that task make progress.
    """
    for _ in range(attempts):
        body = admin_client.get("/api/monitoring/status").json()
        run = body["health_monitor"]["run"]
        if run is not None and not run["running"]:
            return run
        time.sleep(0.05)
    raise AssertionError("probe run did not finish")


class TestMonitoringStatusEndpoint:
    def test_requires_admin(self, client: TestClient, test_user: User) -> None:
        # test_user has no admin Subscription row.
        assert client.get("/api/monitoring/status").status_code == 403

    def test_returns_alert_and_probe_configuration(self, admin_client: TestClient) -> None:
        body = admin_client.get("/api/monitoring/status").json()
        assert set(body) >= {"alerts", "health_monitor", "recipient_configured", "timestamp"}
        assert set(body["alerts"]) >= {"enabled", "pending_groups", "dedupe_minutes"}
        assert set(body["health_monitor"]) >= {"enabled", "interval_seconds", "probes"}

    def test_reports_disabled_when_smtp_is_unconfigured(self, admin_client: TestClient) -> None:
        # The default test environment has no SMTP host, so the pipeline is
        # dormant and the endpoint must say so rather than implying coverage.
        body = admin_client.get("/api/monitoring/status").json()
        assert body["alerts"]["enabled"] is False
        assert body["health_monitor"]["enabled"] is False

    def test_surfaces_probe_state(self, admin_client: TestClient) -> None:
        health_monitor.health_monitor._apply(
            [health_monitor.Observation(key="database", label="PostgreSQL", ok=True, detail="")]
        )
        body = admin_client.get("/api/monitoring/status").json()
        assert body["health_monitor"]["probes"]["database"]["status"] == "up"

    def test_surfaces_the_activity_history(self, admin_client: TestClient) -> None:
        """The Monitoring tab needs what changed, not only what is true now."""
        monitor = health_monitor.health_monitor
        with patch.object(health_monitor.settings, "health_failure_threshold", 1):
            monitor._apply(
                [
                    health_monitor.Observation(
                        key="database", label="PostgreSQL", ok=False, detail="SELECT 1 failed"
                    )
                ]
            )
        monitor.record_event(
            key="bluebubbles",
            label="BlueBubbles inbound webhook",
            status=health_monitor.STATUS_REPAIRED,
            detail="Re-registered the webhook.",
        )

        history = admin_client.get("/api/monitoring/status").json()["health_monitor"]["history"]

        assert [event["status"] for event in history] == ["repaired", "down"]
        assert history[0]["label"] == "BlueBubbles inbound webhook"
        assert set(history[0]) == {"at", "key", "label", "status", "detail"}

    def test_reports_when_probes_last_ran(self, admin_client: TestClient) -> None:
        body = admin_client.get("/api/monitoring/status").json()
        # Nothing has ticked in this process yet, so the tab can say "never"
        # rather than implying a fresh result.
        assert body["health_monitor"]["last_run_at"] is None


class TestTestAlertEndpoint:
    def test_requires_admin(self, client: TestClient) -> None:
        assert client.post("/api/monitoring/test-alert").status_code == 403

    def test_reports_not_sent_when_unconfigured(self, admin_client: TestClient) -> None:
        body = admin_client.post("/api/monitoring/test-alert").json()
        assert body["sent"] is False
        assert "SMTP is not configured" in body["detail"]
        assert body["email"]["configured"] is False

    def test_failure_detail_names_the_transport_problem(self, admin_client: TestClient) -> None:
        """A generic "not sent" sent the operator to the container logs.

        The transport knows why it failed, and a blocked port reads very
        differently from a rejected password. Regression for the 45s test-alert
        that reported nothing but "Not sent".
        """
        with (
            patch.object(email_service.settings, "smtp_host", "smtp.example.com"),
            patch.object(email_service.settings, "smtp_from_email", "ops@example.com"),
            patch.object(email_service.settings, "smtp_port", 587),
            patch.object(admin_alerts.settings, "smtp_host", "smtp.example.com"),
            patch.object(admin_alerts.settings, "smtp_from_email", "ops@example.com"),
            patch.object(admin_alerts.settings, "alert_email", "admin@example.com"),
            patch.object(email_service, "_send_sync", side_effect=TimeoutError("timed out")),
        ):
            body = admin_client.post("/api/monitoring/test-alert").json()
        assert body["sent"] is False
        assert "smtp.example.com:587" in body["detail"]
        assert "blocked" in body["detail"]
        assert body["email"]["last_error"] == body["detail"]

    def test_sends_when_configured(self, admin_client: TestClient) -> None:
        with (
            patch.object(admin_alerts.settings, "smtp_host", "smtp.example.com"),
            patch.object(admin_alerts.settings, "smtp_from_email", "ops@example.com"),
            patch.object(admin_alerts.settings, "alert_email", "admin@example.com"),
            patch.object(
                admin_alerts.email_service,
                "send_admin_alert",
                new_callable=AsyncMock,
                return_value=True,
            ) as send,
        ):
            body = admin_client.post("/api/monitoring/test-alert").json()
        assert body["sent"] is True
        send.assert_awaited_once()


class TestDiagnoseEmailEndpoint:
    def test_requires_admin(self, client: TestClient) -> None:
        assert client.post("/api/monitoring/diagnose-email").status_code == 403

    def test_reports_unconfigured_without_touching_the_network(
        self, admin_client: TestClient
    ) -> None:
        with patch.object(email_service, "_check_port", new_callable=AsyncMock) as check:
            body = admin_client.post("/api/monitoring/diagnose-email").json()
        assert body["configured"] is False
        assert "not configured" in body["conclusion"]
        check.assert_not_awaited()

    def test_names_a_network_level_block_when_no_port_answers(
        self, admin_client: TestClient
    ) -> None:
        """The finding an operator actually needs: nothing is getting out.

        A blocked port and a rejected password both surface as "no email", and
        only one of them is fixable in this repo's config.
        """
        with (
            patch.object(email_service.settings, "smtp_host", "smtp.example.com"),
            patch.object(email_service.settings, "smtp_from_email", "ops@example.com"),
            patch.object(
                email_service,
                "_check_port",
                new=AsyncMock(
                    side_effect=lambda _host, port: email_service.PortCheck(
                        port=port, reachable=False, detail="no response within 5s"
                    )
                ),
            ),
            patch.object(email_service, "_handshake_sync", side_effect=TimeoutError("timed out")),
        ):
            body = admin_client.post("/api/monitoring/diagnose-email").json()
        assert body["handshake_ok"] is False
        assert all(port["reachable"] is False for port in body["ports"])
        assert "network-level block" in body["conclusion"]


class TestRunProbesEndpoint:
    def test_requires_admin(self, client: TestClient) -> None:
        assert client.post("/api/monitoring/run-probes").status_code == 403

    def test_returns_immediately_with_the_steps_it_will_run(self, admin_client: TestClient) -> None:
        """The request must not wait out the run.

        A full pass calls an LLM provider, a residential Mac,
        and one auth_check per specialist per user. Awaiting that is
        what left the admin tab on "Running" with nothing to show.
        """
        observation = health_monitor.Observation(
            key="supplier_pricing", label="Home Depot search", ok=False, detail="zero results"
        )
        with (
            patch.object(
                health_monitor.health_monitor,
                "_collect",
                new_callable=AsyncMock,
                return_value=[observation],
            ),
            patch.object(health_monitor.settings, "health_failure_threshold", 1),
            patch.object(health_monitor.email_service, "send_health_alert", new_callable=AsyncMock),
        ):
            body = admin_client.post("/api/monitoring/run-probes").json()
            assert body["started"] is True
            # Steps are published before any work starts, so the first poll can
            # already show what the run consists of.
            assert body["run"]["running"] is True
            assert [step["key"] for step in body["run"]["steps"]][-2:] == [
                "integrations",
                "alert_email",
            ]
            assert {step["status"] for step in body["run"]["steps"]} == {"pending"}

            run = _await_run(admin_client)
            probes = admin_client.get("/api/monitoring/status").json()["health_monitor"]["probes"]

        assert run["trigger"] == "manual"
        assert probes["supplier_pricing"]["status"] == "down"
        email_step = next(s for s in run["steps"] if s["key"] == "alert_email")
        assert email_step["status"] == "ok"
        assert "Home Depot search DOWN" in email_step["detail"]

    def test_a_second_request_watches_the_run_already_in_flight(
        self, admin_client: TestClient
    ) -> None:
        # Two overlapping passes would double every outbound call and race on
        # the transition bookkeeping.
        with patch.object(health_monitor.health_monitor, "start_run", return_value=False):
            body = admin_client.post("/api/monitoring/run-probes").json()
        assert body["started"] is False
        assert "already in flight" in body["detail"]


def _summary(title: str, count: int = 1, traceback_text: str | None = None) -> AlertSummary:
    now = datetime(2026, 8, 11, 14, 30, 0, tzinfo=UTC)
    return AlertSummary(
        title=title,
        logger_name="backend.app.agent.core",
        level="ERROR",
        count=count,
        message=title,
        traceback_text=traceback_text,
        request_id="abc123",
        first_seen=now,
        last_seen=now,
    )


def _transition(label: str, status: str) -> HealthTransition:
    return HealthTransition(
        key=f"probe:{label}",
        label=label,
        status=status,
        detail="detail text",
        since=datetime(2026, 8, 11, 14, 30, 0, tzinfo=UTC),
        consecutive_failures=2 if status == "down" else 0,
    )


class TestAlertEmailTemplate:
    """Subject lines carry the diagnosis so triage happens in the inbox."""

    def test_single_alert_subject_names_the_error(self) -> None:
        subject = email_service._alert_subject([_summary("SupplierUnavailableError: timeout")])
        assert subject == "[clawbolt] SupplierUnavailableError: timeout"

    def test_repeat_count_appears_in_the_subject(self) -> None:
        subject = email_service._alert_subject([_summary("boom", count=12)])
        assert subject == "[clawbolt] boom (x12)"

    def test_batched_subject_summarizes_groups_and_occurrences(self) -> None:
        subject = email_service._alert_subject([_summary("a", count=2), _summary("b", count=3)])
        assert subject == "[clawbolt] 2 error groups, 5 occurrences"

    def test_message_has_both_text_and_html_parts(self) -> None:
        with patch.object(email_service.settings, "smtp_from_email", "ops@example.com"):
            msg = email_service._admin_alert_message("admin@example.com", [_summary("boom")], 0)
        assert msg["To"] == "admin@example.com"
        types = {part.get_content_type() for part in msg.walk()}
        assert "text/plain" in types
        assert "text/html" in types

    def test_traceback_is_html_escaped(self) -> None:
        with patch.object(email_service.settings, "smtp_from_email", "ops@example.com"):
            msg = email_service._admin_alert_message(
                "admin@example.com",
                [_summary("boom", traceback_text="raise Foo('<script>x</script>')")],
                0,
            )
        html_part = msg.get_body(preferencelist=("html"))
        assert html_part is not None
        rendered = html_part.get_content()
        assert "&lt;script&gt;" in rendered
        assert "<script>" not in rendered

    def test_dropped_group_count_is_surfaced(self) -> None:
        with patch.object(email_service.settings, "smtp_from_email", "ops@example.com"):
            msg = email_service._admin_alert_message("admin@example.com", [_summary("boom")], 7)
        body = msg.get_body(preferencelist=("plain"))
        assert body is not None
        assert "7 additional distinct error group(s) were dropped" in body.get_content()

    async def test_empty_alert_list_sends_nothing(self) -> None:
        assert await email_service.send_admin_alert("admin@example.com", []) is False


class TestHealthEmailTemplate:
    def test_single_down_subject(self) -> None:
        subject = email_service._health_subject([_transition("Home Depot search", "down")])
        assert subject == "[clawbolt] DOWN: Home Depot search"

    def test_single_recovery_subject(self) -> None:
        subject = email_service._health_subject([_transition("Home Depot search", "up")])
        assert subject == "[clawbolt] RECOVERED: Home Depot search"

    def test_multiple_down_subject(self) -> None:
        subject = email_service._health_subject(
            [_transition("a", "down"), _transition("b", "down")]
        )
        assert subject == "[clawbolt] DOWN: 2 systems"

    def test_multiple_recovered_subject(self) -> None:
        subject = email_service._health_subject([_transition("a", "up"), _transition("b", "up")])
        assert subject == "[clawbolt] RECOVERED: 2 systems"

    def test_mixed_subject(self) -> None:
        subject = email_service._health_subject([_transition("a", "down"), _transition("b", "up")])
        assert subject == "[clawbolt] 1 down, 1 recovered"

    def test_message_has_both_parts_and_the_detail(self) -> None:
        with patch.object(email_service.settings, "smtp_from_email", "ops@example.com"):
            msg = email_service._health_alert_message(
                "admin@example.com", [_transition("Home Depot search", "down")]
            )
        types = {part.get_content_type() for part in msg.walk()}
        assert "text/plain" in types
        assert "text/html" in types
        plain = msg.get_body(preferencelist=("plain"))
        assert plain is not None
        content = plain.get_content()
        assert "[DOWN] Home Depot search" in content
        assert "detail text" in content

    async def test_empty_transition_list_sends_nothing(self) -> None:
        assert await email_service.send_health_alert("admin@example.com", []) is False
