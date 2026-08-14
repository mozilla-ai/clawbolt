"""Tests for the ERROR-log to operator-email alert pipeline."""

from __future__ import annotations

import logging
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import admin_alerts


@pytest.fixture(autouse=True)
def _reset_alerts() -> Generator[None]:
    """Detach handlers and clear throttle state between tests."""
    admin_alerts.reset_for_tests()
    yield
    admin_alerts.reset_for_tests()


@pytest.fixture
def alerts_configured() -> Generator[None]:
    """Make alerting look configured without touching real SMTP."""
    with (
        patch.object(admin_alerts.settings, "alerts_enabled", True),
        patch.object(admin_alerts.settings, "smtp_host", "smtp.example.com"),
        patch.object(admin_alerts.settings, "smtp_from_email", "ops@example.com"),
        patch.object(admin_alerts.settings, "alert_email", "admin@example.com"),
        patch.object(admin_alerts.settings, "alert_dedupe_minutes", 30),
        patch.object(admin_alerts.settings, "alert_max_emails_per_hour", 20),
    ):
        yield


@pytest.fixture
def send_mock(alerts_configured: None) -> Generator[AsyncMock]:
    """Patch the SMTP-backed sender and report success."""
    with patch.object(
        admin_alerts.email_service, "send_admin_alert", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


def _log_error(
    logger_name: str, template: str, *args: object, exc: BaseException | None = None
) -> None:
    """Emit one ERROR record through the real logging machinery."""
    target = logging.getLogger(logger_name)
    if exc is not None:
        try:
            raise exc
        except type(exc):
            target.exception(template, *args)
    else:
        target.error(template, *args)


class TestCaptureScope:
    """Which logger trees feed the alert pipeline."""

    def test_app_logger_trees_are_captured(self) -> None:
        assert admin_alerts._should_capture("backend.app.agent.core")
        assert admin_alerts._should_capture("backend.app.routers.admin")

    def test_uvicorn_error_is_captured_for_unhandled_500s(self) -> None:
        # uvicorn.error carries "Exception in ASGI application", which never
        # reaches a backend/ logger.
        assert admin_alerts._should_capture("uvicorn.error")

    def test_third_party_loggers_are_ignored(self) -> None:
        assert not admin_alerts._should_capture("httpx")
        assert not admin_alerts._should_capture("telegram.ext")

    def test_delivery_path_is_excluded_to_prevent_feedback_loop(self) -> None:
        # An SES outage logs ERROR from email_service. Capturing it would
        # enqueue an alert about failing to send alerts, forever.
        assert not admin_alerts._should_capture("backend.app.services.email_service")
        assert not admin_alerts._should_capture("backend.app.services.admin_alerts")

    def test_exclusion_beats_the_broader_inclusion(self) -> None:
        # Both prefixes match email_service; the exclusion must win.
        assert "backend" in admin_alerts.CAPTURED_LOGGERS
        assert not admin_alerts._should_capture("backend.app.services.email_service.inner")


class TestEnablement:
    """Alerting stays dormant unless it can actually deliver."""

    def test_disabled_without_smtp(self) -> None:
        with patch.object(admin_alerts.settings, "smtp_host", ""):
            assert not admin_alerts.is_enabled()

    def test_disabled_without_recipient(self, alerts_configured: None) -> None:
        with (
            patch.object(admin_alerts.settings, "alert_email", ""),
            patch.object(admin_alerts.settings, "admin_email", ""),
        ):
            assert not admin_alerts.is_enabled()

    def test_falls_back_to_admin_email(self, alerts_configured: None) -> None:
        with (
            patch.object(admin_alerts.settings, "alert_email", ""),
            patch.object(admin_alerts.settings, "admin_email", "fallback@example.com"),
        ):
            assert admin_alerts.is_enabled()
            assert admin_alerts._recipient() == "fallback@example.com"

    def test_handler_not_installed_when_disabled(self) -> None:
        with patch.object(admin_alerts.settings, "smtp_host", ""):
            assert admin_alerts.install_alert_handler() is False
        assert not any(
            isinstance(h, admin_alerts.AdminAlertHandler)
            for h in logging.getLogger("backend").handlers
        )

    def test_install_is_idempotent(self, alerts_configured: None) -> None:
        assert admin_alerts.install_alert_handler() is True
        assert admin_alerts.install_alert_handler() is True
        handlers = [
            h
            for h in logging.getLogger("backend").handlers
            if isinstance(h, admin_alerts.AdminAlertHandler)
        ]
        assert len(handlers) == 1


class TestGrouping:
    """Records collapse on logger + exception type + log template."""

    def test_same_template_different_args_is_one_group(self, alerts_configured: None) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.agent.core", "LLM call failed for user %s", "user-a")
        _log_error("backend.app.agent.core", "LLM call failed for user %s", "user-b")
        assert admin_alerts.pending_group_count() == 1

    def test_distinct_templates_are_distinct_groups(self, alerts_configured: None) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.agent.core", "LLM call failed")
        _log_error("backend.app.agent.core", "Tool execution failed")
        assert admin_alerts.pending_group_count() == 2

    def test_warning_level_is_not_captured(self, alerts_configured: None) -> None:
        admin_alerts.install_alert_handler()
        logging.getLogger("backend.app.agent.core").warning("just a warning")
        assert admin_alerts.pending_group_count() == 0

    def test_occurrence_count_accumulates(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        for _ in range(5):
            _log_error("backend.app.x", "boom %s", "arg")
        assert admin_alerts.pending_group_count() == 1

    async def test_count_and_message_reach_the_email(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "search failed for %s", "sku-1")
        _log_error("backend.app.x", "search failed for %s", "sku-2")
        assert await admin_alerts._store.flush() is True

        assert send_mock.await_args is not None
        summaries = send_mock.await_args.args[1]
        assert len(summaries) == 1
        assert summaries[0].count == 2
        # Newest occurrence's rendered message wins.
        assert "sku-2" in summaries[0].message


class TestTracebackCapture:
    def test_exception_type_and_traceback_are_recorded(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "sidecar unreachable", exc=RuntimeError("connection refused"))
        assert admin_alerts.pending_group_count() == 1

    async def test_traceback_text_is_a_string_not_a_live_tuple(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        # Holding exc_info would pin every frame's locals alive for the whole
        # dedupe window; the handler must format to text on capture.
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "sidecar unreachable", exc=RuntimeError("connection refused"))
        await admin_alerts._store.flush()

        assert send_mock.await_args is not None
        summary = send_mock.await_args.args[1][0]
        assert isinstance(summary.traceback_text, str)
        assert "RuntimeError" in summary.traceback_text
        assert "connection refused" in summary.traceback_text

    async def test_exception_type_differentiates_groups(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "call failed", exc=RuntimeError("a"))
        _log_error("backend.app.x", "call failed", exc=ValueError("b"))
        assert admin_alerts.pending_group_count() == 2


class TestThrottling:
    async def test_flush_sends_once_then_dedupes_within_cooldown(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "boom")
        assert await admin_alerts._store.flush() is True
        assert send_mock.await_count == 1

        # Same fingerprint recurs inside the dedupe window: accumulates, no email.
        _log_error("backend.app.x", "boom")
        assert await admin_alerts._store.flush() is False
        assert send_mock.await_count == 1

    async def test_empty_flush_sends_nothing(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        assert await admin_alerts._store.flush() is False
        send_mock.assert_not_awaited()

    async def test_cooldown_expiry_allows_a_resend(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "boom")
        assert await admin_alerts._store.flush() is True

        with patch.object(admin_alerts.settings, "alert_dedupe_minutes", 0):
            _log_error("backend.app.x", "boom")
            assert await admin_alerts._store.flush() is True
        assert send_mock.await_count == 2

    async def test_failed_send_does_not_start_the_cooldown(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        # A transient SES failure must not silence the fingerprint for 30
        # minutes; the next occurrence should try again.
        send_mock.return_value = False
        admin_alerts.install_alert_handler()
        _log_error("backend.app.x", "boom")
        assert await admin_alerts._store.flush() is False

        send_mock.return_value = True
        _log_error("backend.app.x", "boom")
        assert await admin_alerts._store.flush() is True

    async def test_hourly_email_cap_holds_alerts_back(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        with (
            patch.object(admin_alerts.settings, "alert_max_emails_per_hour", 1),
            patch.object(admin_alerts.settings, "alert_dedupe_minutes", 0),
        ):
            _log_error("backend.app.x", "first")
            assert await admin_alerts._store.flush() is True

            _log_error("backend.app.y", "second")
            assert await admin_alerts._store.flush() is False
        # Held back, not discarded.
        assert admin_alerts.pending_group_count() == 1

    async def test_distinct_group_overflow_is_reported_not_silent(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        for i in range(admin_alerts._MAX_PENDING_GROUPS + 10):
            _log_error("backend.app.x", f"unique failure {i}")
        assert admin_alerts.pending_group_count() == admin_alerts._MAX_PENDING_GROUPS

        await admin_alerts._store.flush()
        assert send_mock.await_args is not None
        dropped = send_mock.await_args.args[2]
        assert dropped == 10

    async def test_batch_is_capped_per_email(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        admin_alerts.install_alert_handler()
        for i in range(admin_alerts._MAX_GROUPS_PER_EMAIL + 5):
            _log_error("backend.app.x", f"failure {i}")
        await admin_alerts._store.flush()
        assert send_mock.await_args is not None
        summaries = send_mock.await_args.args[1]
        assert len(summaries) == admin_alerts._MAX_GROUPS_PER_EMAIL


class TestHandlerRobustness:
    def test_emit_never_raises(self, alerts_configured: None) -> None:
        handler = admin_alerts.AdminAlertHandler()
        record = logging.LogRecord(
            name="backend.app.x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="%s %s",
            args=("only-one-arg",),  # deliberately malformed: getMessage() raises
            exc_info=None,
        )
        with patch.object(handler, "handleError") as handle_error:
            handler.emit(record)
        handle_error.assert_called_once()

    async def test_test_alert_bypasses_dedupe(
        self, alerts_configured: None, send_mock: AsyncMock
    ) -> None:
        assert await admin_alerts.send_test_alert() is True
        assert await admin_alerts.send_test_alert() is True
        assert send_mock.await_count == 2

    async def test_test_alert_is_a_noop_when_disabled(self) -> None:
        with patch.object(admin_alerts.settings, "smtp_host", ""):
            assert await admin_alerts.send_test_alert() is False
