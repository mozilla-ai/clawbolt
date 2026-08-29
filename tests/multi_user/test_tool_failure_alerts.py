"""Tests for routing failed agent tool calls into the operator alert email.

Covers the three decisions the feature turns on: which ``ToolErrorKind`` values
count as incidents, how data-sharing consent gates detail without hiding the
occurrence, and that a storm collapses into one grouped line.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.tool_failure_hook import (
    TOOL_FAILURE_SCHEMA_VERSION,
    ToolFailurePayload,
    get_tool_failure_handler,
    report_tool_failure,
    set_tool_failure_handler,
)
from backend.app.agent.tools.base import ToolErrorKind
from backend.app.services import admin_alerts, tool_failure_alerts


@pytest.fixture(autouse=True)
def _reset() -> Generator[None]:
    admin_alerts.reset_for_tests()
    tool_failure_alerts.reset_for_tests()
    set_tool_failure_handler(None)
    yield
    admin_alerts.reset_for_tests()
    tool_failure_alerts.reset_for_tests()
    set_tool_failure_handler(None)


@pytest.fixture
def alerts_configured() -> Generator[None]:
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
    with patch.object(
        admin_alerts.email_service, "send_admin_alert", new_callable=AsyncMock
    ) as mock:
        mock.return_value = True
        yield mock


def _payload(
    *,
    user_id: str = "user-1",
    tool_name: str = "qb_query",
    kind: ToolErrorKind = ToolErrorKind.SERVICE,
    args: dict | None = None,
    result: str = "QuickBooks returned 503",
) -> ToolFailurePayload:
    return ToolFailurePayload(
        schema_version=TOOL_FAILURE_SCHEMA_VERSION,
        user_id=user_id,
        tool_name=tool_name,
        error_kind=str(kind),
        args=args if args is not None else {"entity": "Invoice"},
        result_text=result,
    )


def _consent(user_id: str, consented: bool) -> None:
    """Seed the consent cache so no database read is needed."""
    tool_failure_alerts._store_consent(user_id, consented)


# ---------------------------------------------------------------------------
# The hook seam
# ---------------------------------------------------------------------------


class TestHook:
    @pytest.mark.asyncio
    async def test_reporting_without_a_handler_is_a_no_op(self) -> None:
        """Single-user deployments and CI must pay nothing for this."""
        await report_tool_failure(_payload())  # must not raise

    @pytest.mark.asyncio
    async def test_a_raising_handler_never_reaches_the_agent_loop(self) -> None:
        """A reporting bug must not break the user's turn."""

        async def boom(payload: ToolFailurePayload) -> None:
            raise RuntimeError("handler is broken")

        set_tool_failure_handler(boom)
        await report_tool_failure(_payload())  # must not raise

    def test_install_registers_the_handler(self) -> None:
        tool_failure_alerts.install_tool_failure_alerts()
        assert get_tool_failure_handler() is tool_failure_alerts.handle_tool_failure


# ---------------------------------------------------------------------------
# Which failures count
# ---------------------------------------------------------------------------


class TestKindFiltering:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind", [ToolErrorKind.INTERNAL, ToolErrorKind.SERVICE, ToolErrorKind.AUTH]
    )
    async def test_real_faults_are_recorded(
        self, kind: ToolErrorKind, alerts_configured: None
    ) -> None:
        _consent("user-1", False)
        await tool_failure_alerts.handle_tool_failure(_payload(kind=kind))
        assert admin_alerts._store.pending_count() == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind",
        [
            ToolErrorKind.VALIDATION,
            ToolErrorKind.NOT_FOUND,
            ToolErrorKind.PERMISSION,
            ToolErrorKind.INTERRUPTED,
        ],
    )
    async def test_agent_and_user_driven_kinds_are_ignored(
        self, kind: ToolErrorKind, alerts_configured: None
    ) -> None:
        """VALIDATION fires constantly by design (the hint tells the model to
        retry) and PERMISSION is the user declining. Alerting on either trains
        the operator to ignore the channel."""
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(_payload(kind=kind))
        assert admin_alerts._store.pending_count() == 0

    @pytest.mark.asyncio
    async def test_nothing_is_recorded_when_alerting_is_unconfigured(self) -> None:
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(_payload())
        assert admin_alerts._store.pending_count() == 0


# ---------------------------------------------------------------------------
# Consent gating
# ---------------------------------------------------------------------------


class TestConsentGating:
    @pytest.mark.asyncio
    async def test_consenting_user_contributes_detail(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(
            _payload(args={"entity": "Invoice"}, result="token revoked")
        )
        await admin_alerts._store.flush()

        failures = send_mock.call_args.kwargs["tool_failures"]
        assert failures[0].samples
        assert "Invoice" in failures[0].samples[0]
        assert failures[0].consented_user_count == 1

    @pytest.mark.asyncio
    async def test_non_consenting_user_counts_but_shows_nothing(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        """The whole point of counting them: an outage confined to
        non-consenting users must not be invisible."""
        _consent("user-1", False)
        await tool_failure_alerts.handle_tool_failure(
            _payload(args={"entity": "SecretCustomer"}, result="token revoked")
        )
        await admin_alerts._store.flush()

        failures = send_mock.call_args.kwargs["tool_failures"]
        assert failures[0].count == 1
        assert failures[0].user_count == 1
        assert failures[0].consented_user_count == 0
        assert failures[0].samples == []

    @pytest.mark.asyncio
    async def test_no_argument_text_leaks_for_a_non_consenting_user(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        """Assert on the rendered email, not just the summary: this is the
        thing that would actually be a privacy incident."""
        from backend.app.services.email_service import _admin_alert_message

        _consent("user-1", False)
        await tool_failure_alerts.handle_tool_failure(
            _payload(args={"customer": "Wayne Enterprises"}, result="boom")
        )
        await admin_alerts._store.flush()

        failures = send_mock.call_args.kwargs["tool_failures"]
        rendered = str(_admin_alert_message("admin@example.com", [], 0, failures))
        assert "Wayne Enterprises" not in rendered

    @pytest.mark.asyncio
    async def test_unknown_consent_fails_closed(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        """A cache miss withholds detail rather than blocking the agent loop
        on a database read or assuming consent."""
        with patch.object(tool_failure_alerts, "_warm_consent", new_callable=AsyncMock):
            await tool_failure_alerts.handle_tool_failure(
                _payload(user_id="never-seen", args={"customer": "Acme"})
            )
        await admin_alerts._store.flush()

        failures = send_mock.call_args.kwargs["tool_failures"]
        assert failures[0].count == 1
        assert failures[0].samples == []

    @pytest.mark.asyncio
    async def test_mixed_consent_reports_both_populations(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        _consent("yes-1", True)
        _consent("no-1", False)
        _consent("no-2", False)
        for uid in ("yes-1", "no-1", "no-2"):
            await tool_failure_alerts.handle_tool_failure(_payload(user_id=uid))
        await admin_alerts._store.flush()

        summary = send_mock.call_args.kwargs["tool_failures"][0]
        assert summary.count == 3
        assert summary.user_count == 3
        assert summary.consented_user_count == 1


# ---------------------------------------------------------------------------
# Grouping and storm control
# ---------------------------------------------------------------------------


class TestGrouping:
    @pytest.mark.asyncio
    async def test_a_storm_collapses_into_one_group(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        """40 users hitting one revoked token is one line, not 40 emails."""
        for i in range(40):
            _consent(f"user-{i}", False)
            await tool_failure_alerts.handle_tool_failure(
                _payload(user_id=f"user-{i}", kind=ToolErrorKind.AUTH)
            )
        await admin_alerts._store.flush()

        assert send_mock.call_count == 1
        failures = send_mock.call_args.kwargs["tool_failures"]
        assert len(failures) == 1
        assert failures[0].count == 40
        assert failures[0].user_count == 40

    @pytest.mark.asyncio
    async def test_distinct_tools_are_distinct_groups(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        """The gap this feature fills: the ERROR-log path fingerprints on the
        log template, so every crashing tool collapses into one group."""
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(_payload(tool_name="qb_query"))
        await tool_failure_alerts.handle_tool_failure(_payload(tool_name="web_search"))
        await admin_alerts._store.flush()

        names = {f.tool_name for f in send_mock.call_args.kwargs["tool_failures"]}
        assert names == {"qb_query", "web_search"}

    @pytest.mark.asyncio
    async def test_same_tool_different_kinds_are_distinct_groups(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(_payload(kind=ToolErrorKind.AUTH))
        await tool_failure_alerts.handle_tool_failure(_payload(kind=ToolErrorKind.SERVICE))
        await admin_alerts._store.flush()

        kinds = {f.error_kind for f in send_mock.call_args.kwargs["tool_failures"]}
        assert kinds == {str(ToolErrorKind.AUTH), str(ToolErrorKind.SERVICE)}

    @pytest.mark.asyncio
    async def test_samples_are_bounded(self, send_mock: AsyncMock, alerts_configured: None) -> None:
        """Twenty copies of one broken call tells you nothing the first did."""
        for i in range(20):
            _consent(f"user-{i}", True)
            await tool_failure_alerts.handle_tool_failure(
                _payload(user_id=f"user-{i}", args={"entity": f"Invoice{i}"})
            )
        await admin_alerts._store.flush()

        summary = send_mock.call_args.kwargs["tool_failures"][0]
        assert summary.count == 20
        assert len(summary.samples) <= admin_alerts._MAX_SAMPLES_PER_GROUP

    @pytest.mark.asyncio
    async def test_identical_samples_are_not_repeated(
        self, send_mock: AsyncMock, alerts_configured: None
    ) -> None:
        _consent("user-1", True)
        for _ in range(5):
            await tool_failure_alerts.handle_tool_failure(_payload())
        await admin_alerts._store.flush()

        assert len(send_mock.call_args.kwargs["tool_failures"][0].samples) == 1

    @pytest.mark.asyncio
    async def test_a_failed_send_puts_the_group_back(self, alerts_configured: None) -> None:
        """Unlike an ERROR log, a tool failure has no second record anywhere,
        so a dropped batch is the only copy."""
        _consent("user-1", True)
        await tool_failure_alerts.handle_tool_failure(_payload())

        with patch.object(
            admin_alerts.email_service, "send_admin_alert", new_callable=AsyncMock
        ) as failing:
            failing.return_value = False
            assert await admin_alerts._store.flush() is False

        assert admin_alerts._store.pending_count() == 1


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def _summary(self, **kw: object) -> admin_alerts.ToolFailureSummary:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        base: dict = {
            "tool_name": "qb_query",
            "error_kind": "auth",
            "count": 14,
            "user_count": 4,
            "consented_user_count": 1,
            "samples": ["args={entity=Invoice} -> token revoked"],
            "first_seen": now,
            "last_seen": now,
        }
        base.update(kw)
        return admin_alerts.ToolFailureSummary(**base)

    def test_tool_only_email_renders_without_application_errors(self) -> None:
        from backend.app.services.email_service import _admin_alert_message

        msg = _admin_alert_message("admin@example.com", [], 0, [self._summary()])
        rendered = str(msg)
        assert "qb_query" in rendered
        assert "Tool failures" in rendered

    def test_subject_names_the_tool_when_it_is_the_only_problem(self) -> None:
        from backend.app.services.email_service import _alert_subject

        subject = _alert_subject([], [self._summary()])
        assert "qb_query" in subject
        assert "x14" in subject

    def test_withheld_user_count_is_stated(self) -> None:
        from backend.app.services.email_service import _admin_alert_message

        msg = _admin_alert_message("admin@example.com", [], 0, [self._summary()])
        # 4 users seen, 1 consented, so 3 are counted but not shown.
        assert "3 further user(s) have not opted into data sharing" in str(msg)

    @pytest.mark.asyncio
    async def test_send_is_skipped_when_there_is_nothing_to_report(self) -> None:
        from backend.app.services.email_service import send_admin_alert

        assert await send_admin_alert("admin@example.com", [], 0, []) is False
