"""Tests for proactive dependency health probes and transition-based alerting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.channels.bluebubbles import (
    BlueBubblesChannel,
    BlueBubblesHealth,
    WebhookCheck,
)
from backend.app.models import Subscription, User
from backend.app.services import health_monitor as hm

_STEP_INTEGRATIONS_KEY = hm._STEP_INTEGRATIONS


@pytest.fixture
def monitor() -> hm.HealthMonitor:
    return hm.HealthMonitor()


@pytest.fixture(autouse=True)
def _reset_repair_cooldown() -> Generator[None]:
    """The repair cooldown and attempt counter are module state.

    Leaking the cooldown silences later tests; leaking the counter makes them
    stop repairing altogether.
    """
    hm._repair_notice_sent_at = None
    hm._repair_attempts = 0
    yield
    hm._repair_notice_sent_at = None
    hm._repair_attempts = 0


@pytest.fixture
def alerts_configured() -> Generator[None]:
    with (
        patch.object(hm.settings, "health_monitor_enabled", True),
        patch.object(hm.settings, "smtp_host", "smtp.example.com"),
        patch.object(hm.settings, "smtp_from_email", "ops@example.com"),
        patch.object(hm.settings, "alert_email", "admin@example.com"),
        patch.object(hm.settings, "health_failure_threshold", 2),
    ):
        yield


def _steps(progress: dict[str, object] | None) -> list[dict[str, object]]:
    """Typed accessor for the step list inside a run-progress dict."""
    assert progress is not None
    steps = progress["steps"]
    assert isinstance(steps, list)
    return cast("list[dict[str, object]]", steps)


def _obs(key: str, ok: bool, *, first_alert: bool = True, detail: str = "") -> hm.Observation:
    return hm.Observation(
        key=key,
        label=key,
        ok=ok,
        detail=detail,
        alert_on_first_observation=first_alert,
    )


class TestTransitionStateMachine:
    """Alerts fire on status changes, never on steady state."""

    def test_healthy_first_observation_is_silent(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        assert monitor._apply([_obs("database", True)]) == []
        assert monitor.snapshot()["database"]["status"] == hm.STATUS_UP

    def test_failure_below_threshold_does_not_alert(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        # One timed-out probe against a residential host is noise, not an outage.
        assert monitor._apply([_obs("bluebubbles", False)]) == []
        assert monitor.snapshot()["bluebubbles"]["status"] == hm.STATUS_UNKNOWN

    def test_failure_at_threshold_alerts_once(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply([_obs("bluebubbles", False)])
        transitions = monitor._apply([_obs("bluebubbles", False, detail="host silent")])
        assert len(transitions) == 1
        assert transitions[0].status == hm.STATUS_DOWN
        assert transitions[0].consecutive_failures == 2
        assert transitions[0].detail == "host silent"

    def test_steady_state_down_stays_silent(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply([_obs("bluebubbles", False)])
        assert len(monitor._apply([_obs("bluebubbles", False)])) == 1
        # A six-hour outage must not be 72 emails.
        for _ in range(10):
            assert monitor._apply([_obs("bluebubbles", False)]) == []

    def test_recovery_emits_an_up_transition(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply([_obs("bluebubbles", False)])
        monitor._apply([_obs("bluebubbles", False)])
        transitions = monitor._apply([_obs("bluebubbles", True)])
        assert len(transitions) == 1
        assert transitions[0].status == hm.STATUS_UP
        assert monitor.snapshot()["bluebubbles"]["status"] == hm.STATUS_UP

    def test_recovery_is_only_emitted_once(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply([_obs("x", False)])
        monitor._apply([_obs("x", False)])
        assert len(monitor._apply([_obs("x", True)])) == 1
        assert monitor._apply([_obs("x", True)]) == []

    def test_intermittent_success_resets_the_failure_counter(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply([_obs("x", False)])
        monitor._apply([_obs("x", True)])
        # Counter reset, so the next single failure is again below threshold.
        assert monitor._apply([_obs("x", False)]) == []

    def test_threshold_of_one_alerts_immediately(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with patch.object(hm.settings, "health_failure_threshold", 1):
            assert len(monitor._apply([_obs("database", False)])) == 1

    def test_multiple_probes_transition_independently(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with patch.object(hm.settings, "health_failure_threshold", 1):
            transitions = monitor._apply([_obs("database", False), _obs("llm", False)])
        assert {t.key for t in transitions} == {"database", "llm"}


class TestBaselineSeeding:
    """A never-connected integration is a user choice, not an outage."""

    def test_first_observation_failure_is_silent_for_integrations(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        key = "integration:quickbooks:user-1"
        with patch.object(hm.settings, "health_failure_threshold", 1):
            assert monitor._apply([_obs(key, False, first_alert=False)]) == []
        # State still records DOWN so the eventual recovery is reportable.
        assert monitor.snapshot()[key]["status"] == hm.STATUS_DOWN

    def test_connecting_later_reports_recovery(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        key = "integration:quickbooks:user-1"
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs(key, False, first_alert=False)])
            transitions = monitor._apply([_obs(key, True, first_alert=False)])
        assert len(transitions) == 1
        assert transitions[0].status == hm.STATUS_UP

    def test_token_that_worked_then_lapsed_does_alert(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        # The failure mode that matters: authenticated, then not.
        key = "integration:quickbooks:user-1"
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs(key, True, first_alert=False)])
            transitions = monitor._apply(
                [_obs(key, False, first_alert=False, detail="token expired")]
            )
        assert len(transitions) == 1
        assert transitions[0].status == hm.STATUS_DOWN
        assert transitions[0].detail == "token expired"

    def test_infrastructure_alerts_on_first_observation(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        # A DB that is down at boot has no ambiguous baseline.
        with patch.object(hm.settings, "health_failure_threshold", 1):
            assert len(monitor._apply([_obs("database", False)])) == 1

    def test_never_connected_integrations_are_marked_as_such(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        """DOWN, but not breakage, and the admin view must be able to tell them apart.

        8 specialist integrations across 50 users would otherwise render as
        several hundred failures on a completely healthy deployment.
        """
        key = "integration:quickbooks:user-1"
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs(key, False, first_alert=False)])

        entry = monitor.snapshot()[key]
        assert entry["status"] == hm.STATUS_DOWN
        assert entry["never_connected"] is True

    def test_an_integration_that_worked_and_broke_is_not_never_connected(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        """The distinction is "was it ever up", not "is it silent"."""
        key = "integration:quickbooks:user-1"
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs(key, True, first_alert=False)])
            monitor._apply([_obs(key, False, first_alert=False, detail="token expired")])

        entry = monitor.snapshot()[key]
        assert entry["status"] == hm.STATUS_DOWN
        assert entry["never_connected"] is False

    def test_infrastructure_down_is_never_marked_never_connected(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs("database", False)])

        assert monitor.snapshot()["database"]["never_connected"] is False


class TestProbeCollection:
    async def test_probe_exception_becomes_a_down_observation(
        self, monitor: hm.HealthMonitor
    ) -> None:
        async def _boom() -> hm.Observation:
            raise RuntimeError("probe blew up")

        probe = hm._InfraProbe("exploding", _boom)
        with (
            patch.object(monitor, "_infra_probes", return_value=[probe]),
            patch.object(hm, "_probe_integrations", new_callable=AsyncMock, return_value=[]),
        ):
            observations = await monitor._collect()
        assert len(observations) == 1
        assert observations[0].key == "exploding"
        assert observations[0].ok is False
        assert "probe blew up" in observations[0].detail

    async def test_integration_sweep_failure_does_not_lose_infra_results(
        self, monitor: hm.HealthMonitor
    ) -> None:
        async def _ok() -> hm.Observation:
            return _obs("database", True)

        probe = hm._InfraProbe("database", _ok)
        with (
            patch.object(monitor, "_infra_probes", return_value=[probe]),
            patch.object(
                hm,
                "_probe_integrations",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db gone"),
            ),
        ):
            observations = await monitor._collect()
        assert [o.key for o in observations] == ["database"]

    def test_unconfigured_dependencies_are_not_probed(self, monitor: hm.HealthMonitor) -> None:
        with (
            patch.object(hm.settings, "bluebubbles_server_url", ""),
            patch.object(hm.settings, "health_probe_llm", False),
        ):
            names = [p.name for p in monitor._infra_probes()]
        assert names == ["database"]

    def test_configured_dependencies_are_probed(self, monitor: hm.HealthMonitor) -> None:
        with (
            patch.object(hm.settings, "bluebubbles_server_url", "http://mac.local"),
            patch.object(hm.settings, "bluebubbles_password", "secret"),
            patch.object(hm.settings, "llm_model", "some-model"),
            patch.object(hm.settings, "health_probe_llm", True),
        ):
            names = [p.name for p in monitor._infra_probes()]
        assert names == [
            "database",
            "llm",
            "bluebubbles",
            "supplier_sidecar",
        ]


class TestDatabaseProbe:
    async def test_reuses_the_oss_health_handler(self) -> None:
        with patch.object(
            hm,
            "health_check",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="ok", database="ok"),
        ):
            observation = await hm._probe_database()
        assert observation.ok is True

    async def test_reports_down_when_oss_says_error(self) -> None:
        with patch.object(
            hm,
            "health_check",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(status="degraded", database="error"),
        ):
            observation = await hm._probe_database()
        assert observation.ok is False


def _bb_channel(**health_kwargs: object) -> BlueBubblesChannel:
    """A channel carrying a pre-seeded probe result, as the OSS health loop leaves it."""
    channel = BlueBubblesChannel()
    channel.last_health = BlueBubblesHealth(
        reachable=True,
        authenticated=True,
        imessage_signed_in=True,
        **health_kwargs,  # type: ignore[arg-type]
    )
    return channel


@contextmanager
def _bb_manager(channel: object) -> Generator[None]:
    manager = MagicMock()
    manager.channels = {"bluebubbles": channel} if channel is not None else {}
    with patch.object(hm, "get_manager", return_value=manager):
        yield


class TestBlueBubblesProbe:
    """Reachable is necessary but nowhere near sufficient.

    The bridge can answer while signed out of iMessage (no send) or with no
    webhook registered (no receive), and neither of those raises anything.
    """

    async def test_healthy_bridge_with_inbound_registered(self) -> None:
        # Reuses the OSS health loop's result rather than re-polling the
        # operator's Mac.
        with (
            _bb_manager(_bb_channel(server_version="1.9.8")),
            patch.object(hm, "_check_inbound_webhook", new_callable=AsyncMock, return_value=""),
        ):
            observation = await hm._probe_bluebubbles()

        assert observation.ok is True
        assert "1.9.8" in observation.detail

    async def test_unreachable_bridge_reports_the_probe_detail(self) -> None:
        channel = BlueBubblesChannel()
        channel.last_health = BlueBubblesHealth(
            reachable=False, authenticated=False, detail="ConnectError contacting the server"
        )
        with _bb_manager(channel):
            observation = await hm._probe_bluebubbles()

        assert observation.ok is False
        assert "ConnectError" in observation.detail

    async def test_rejected_password_is_down_not_healthy(self) -> None:
        """Regression: a 401 used to read as reachable, so this probe stayed green."""
        channel = BlueBubblesChannel()
        channel.last_health = BlueBubblesHealth(
            reachable=True, authenticated=False, detail="BlueBubbles rejected the server password"
        )
        with _bb_manager(channel):
            observation = await hm._probe_bluebubbles()

        assert observation.ok is False
        assert "password" in observation.detail

    async def test_signed_out_mac_is_down_despite_a_reachable_bridge(self) -> None:
        channel = _bb_channel()
        channel.last_health = BlueBubblesHealth(
            reachable=True, authenticated=True, imessage_signed_in=False
        )
        with _bb_manager(channel):
            observation = await hm._probe_bluebubbles()

        assert observation.ok is False
        assert "cannot send" in observation.detail

    async def test_missing_inbound_webhook_is_down(self) -> None:
        with (
            _bb_manager(_bb_channel()),
            patch.object(
                hm,
                "_check_inbound_webhook",
                new_callable=AsyncMock,
                return_value="Re-registered the webhook.",
            ),
        ):
            observation = await hm._probe_bluebubbles()

        assert observation.ok is False
        assert "Re-registered" in observation.detail

    async def test_probes_directly_when_the_oss_health_loop_is_disabled(self) -> None:
        """A disabled OSS loop leaves ``last_health`` frozen at boot forever."""
        channel = _bb_channel()
        fresh = BlueBubblesHealth(reachable=True, authenticated=True, imessage_signed_in=True)
        with (
            _bb_manager(channel),
            patch.object(hm.settings, "bluebubbles_health_check_interval_seconds", 0),
            patch.object(
                channel, "check_health", new_callable=AsyncMock, return_value=fresh
            ) as mock_check,
            patch.object(hm, "_check_inbound_webhook", new_callable=AsyncMock, return_value=""),
        ):
            assert (await hm._probe_bluebubbles()).ok is True

        mock_check.assert_awaited_once()

    async def test_missing_channel_is_down(self) -> None:
        with _bb_manager(None):
            assert (await hm._probe_bluebubbles()).ok is False


class TestInboundWebhookCheck:
    """The failure nothing else catches: registration silently never landed."""

    @pytest.fixture
    def deployed(self) -> Generator[None]:
        # ``_last_run_at`` is set because the inbound check is deliberately
        # skipped on the process's first tick; see the startup-race test.
        with (
            patch.object(hm.settings, "app_base_url", "https://clawbolt.example"),
            patch.object(hm.settings, "bluebubbles_server_url", "http://bb.example"),
            patch.object(hm.settings, "bluebubbles_password", "bb-pw"),
            patch.object(hm.health_monitor, "_last_run_at", datetime.now(UTC)),
        ):
            yield

    async def test_registered_webhook_passes(self, deployed: None) -> None:
        with patch.object(
            hm,
            "verify_webhook_registration",
            new_callable=AsyncMock,
            return_value=WebhookCheck(ok=True, listed=True),
        ):
            assert await hm._check_inbound_webhook() == ""

    async def test_local_base_url_is_not_checked(self) -> None:
        """The lifespan does not register a webhook locally, so this must not be red."""
        with (
            patch.object(hm.settings, "app_base_url", "http://localhost:8000"),
            patch.object(hm, "verify_webhook_registration", new_callable=AsyncMock) as mock_verify,
        ):
            assert await hm._check_inbound_webhook() == ""

        mock_verify.assert_not_awaited()

    async def test_missing_registration_is_repaired_and_emailed(self, deployed: None) -> None:
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                return_value=WebhookCheck(
                    ok=False, listed=True, detail="no webhooks are registered"
                ),
            ),
            patch.object(
                hm, "register_bluebubbles_webhook", new_callable=AsyncMock, return_value=True
            ) as mock_register,
            patch.object(
                hm.email_service, "send_repair_notice", new_callable=AsyncMock, return_value=True
            ) as mock_email,
        ):
            detail = await hm._check_inbound_webhook()

        assert "Re-registered" in detail
        mock_register.assert_awaited_once()
        mock_email.assert_awaited_once()
        # The repair email is the only alert this can produce: it resolves the
        # failure before the consecutive-failure threshold is ever reached.
        assert "no webhooks are registered" in mock_email.call_args.kwargs["problem"]

    async def test_failed_repair_says_inbound_is_still_down(self, deployed: None) -> None:
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                return_value=WebhookCheck(
                    ok=False, listed=True, detail="no webhooks are registered"
                ),
            ),
            patch.object(
                hm, "register_bluebubbles_webhook", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                hm.email_service, "send_repair_notice", new_callable=AsyncMock, return_value=True
            ),
        ):
            detail = await hm._check_inbound_webhook()

        assert "still down" in detail

    async def test_unlistable_webhooks_are_not_repaired(self, deployed: None) -> None:
        """We could not ask, so we do not know it is missing: no writes on a guess."""
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                return_value=WebhookCheck(ok=False, listed=False, detail="could not list webhooks"),
            ),
            patch.object(hm, "register_bluebubbles_webhook", new_callable=AsyncMock) as mock_reg,
        ):
            detail = await hm._check_inbound_webhook()

        assert "unverified" in detail
        mock_reg.assert_not_awaited()

    async def test_repair_email_is_rate_limited(self, deployed: None) -> None:
        """A webhook that keeps vanishing must not email on every tick."""
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                return_value=WebhookCheck(
                    ok=False, listed=True, detail="no webhooks are registered"
                ),
            ),
            patch.object(
                hm, "register_bluebubbles_webhook", new_callable=AsyncMock, return_value=True
            ),
            patch.object(hm.settings, "alert_dedupe_minutes", 30),
            patch.object(
                hm.email_service, "send_repair_notice", new_callable=AsyncMock, return_value=True
            ) as mock_email,
        ):
            await hm._check_inbound_webhook()
            await hm._check_inbound_webhook()
            await hm._check_inbound_webhook()

        assert mock_email.await_count == 1

    async def test_repairs_stop_after_the_cap_instead_of_looping(self, deployed: None) -> None:
        """Registration is delete-then-POST, so an endless retry is not harmless.

        Each attempt reopens a window with no webhook registered at all, and the
        email cooldown means the operator hears about it once. Past the cap the
        probe stops writing to the Mac and reports a plain failure, which
        escalates through the ordinary DOWN transition instead.
        """
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                return_value=WebhookCheck(
                    ok=False, listed=True, detail="no webhooks are registered"
                ),
            ),
            patch.object(
                hm, "register_bluebubbles_webhook", new_callable=AsyncMock, return_value=True
            ) as mock_register,
            patch.object(
                hm.email_service, "send_repair_notice", new_callable=AsyncMock, return_value=True
            ),
        ):
            details = [await hm._check_inbound_webhook() for _ in range(6)]

        assert mock_register.await_count == hm._MAX_CONSECUTIVE_REPAIRS
        assert "no further automatic attempts" in details[-1]

    async def test_a_passing_check_resets_the_repair_budget(self, deployed: None) -> None:
        """A webhook that breaks, is fixed, and breaks again months later still repairs."""
        failing = WebhookCheck(ok=False, listed=True, detail="no webhooks are registered")
        passing = WebhookCheck(ok=True, listed=True)
        with (
            patch.object(
                hm,
                "verify_webhook_registration",
                new_callable=AsyncMock,
                side_effect=[failing, failing, passing, failing],
            ),
            patch.object(
                hm, "register_bluebubbles_webhook", new_callable=AsyncMock, return_value=True
            ) as mock_register,
            patch.object(hm.email_service, "send_repair_notice", new_callable=AsyncMock),
        ):
            for _ in range(4):
                await hm._check_inbound_webhook()

        assert mock_register.await_count == 3
        assert hm._repair_attempts == 1

    async def test_first_tick_skips_the_inbound_check(self) -> None:
        """The lifespan's own registration may still be in flight on tick one.

        That path deletes the previous deploy's webhook before POSTing the new
        one, so a check landing in the gap would repair and email about a deploy
        where nothing was ever broken.
        """
        with (
            patch.object(hm.settings, "app_base_url", "https://clawbolt.example"),
            patch.object(hm.settings, "bluebubbles_server_url", "http://bb.example"),
            patch.object(hm.health_monitor, "_last_run_at", None),
            patch.object(hm, "verify_webhook_registration", new_callable=AsyncMock) as mock_verify,
            patch.object(hm, "register_bluebubbles_webhook", new_callable=AsyncMock) as mock_reg,
        ):
            assert await hm._check_inbound_webhook() == ""

        mock_verify.assert_not_awaited()
        mock_reg.assert_not_awaited()


class TestSupplierSidecarProbe:
    """The background probe must never send traffic to a retailer."""

    @pytest.fixture
    def sidecar(self) -> Generator[MagicMock]:
        mock = MagicMock()
        mock.healthy = AsyncMock(return_value=True)
        mock.search_products = AsyncMock()
        with patch.object(hm, "_supplier_sidecar", return_value=mock):
            yield mock

    async def test_healthy_browser_is_up_without_a_search(self, sidecar: MagicMock) -> None:
        observation = await hm._probe_supplier_sidecar()
        assert observation.key == "supplier_sidecar"
        assert observation.ok is True
        sidecar.healthy.assert_awaited_once()
        sidecar.search_products.assert_not_awaited()

    async def test_unhealthy_browser_is_down_without_a_search(self, sidecar: MagicMock) -> None:
        sidecar.healthy.return_value = False
        observation = await hm._probe_supplier_sidecar()
        assert observation.ok is False
        assert "No retailer search" in observation.detail
        sidecar.search_products.assert_not_awaited()


@contextmanager
def _integration_sweep(
    users: list[SimpleNamespace],
    registry: MagicMock,
    labels: dict[str, str] | None = None,
) -> Generator[None]:
    """Wire the sweep's three collaborators: users, registry, subscription emails."""
    store = MagicMock()
    store.list_all_async = AsyncMock(return_value=users)
    with (
        patch.object(hm, "get_user_store", return_value=store),
        patch.object(hm, "default_registry", registry),
        patch.object(hm, "_user_labels", new=AsyncMock(return_value=labels or {})),
    ):
        yield


def _registry(names: set[str], **kwargs: object) -> MagicMock:
    registry = MagicMock()
    registry.specialist_factory_names = names
    registry.get_unauthenticated_specialists = AsyncMock(**kwargs)
    return registry


class TestIntegrationProbe:
    async def test_emits_one_baseline_silent_key_per_user_and_factory(self) -> None:
        registry = _registry(
            {"quickbooks", "calendar"}, return_value={"quickbooks": "not connected"}
        )
        with _integration_sweep([SimpleNamespace(id="user-1", user_id="alice")], registry):
            observations = await hm._probe_integrations()

        by_key = {o.key: o for o in observations}
        assert by_key["integration:calendar:user-1"].ok is True
        assert by_key["integration:quickbooks:user-1"].ok is False
        assert by_key["integration:quickbooks:user-1"].detail == "not connected"
        # Never alerts on the baseline, only on UP -> DOWN.
        assert all(
            o.alert_on_first_observation is False
            for o in observations
            if o.key.startswith(hm._INTEGRATION_PREFIX)
        )

    async def test_carries_the_grouping_metadata_the_admin_tab_needs(self) -> None:
        """The tab groups on these fields rather than splitting the probe key.

        A key is an internal identifier: parsing ``integration:<name>:<id>``
        back apart in the UI breaks the moment either half contains a colon.
        """
        registry = _registry({"quickbooks"}, return_value={})
        with _integration_sweep(
            [SimpleNamespace(id="user-1", user_id="google_123")],
            registry,
            labels={"user-1": "alice@example.com"},
        ):
            observations = await hm._probe_integrations()

        row = next(o for o in observations if o.key.startswith(hm._INTEGRATION_PREFIX))
        assert (row.user_id, row.user_label, row.integration) == (
            "user-1",
            "alice@example.com",
            "quickbooks",
        )
        # The label names the tenant an admin can act on, not a UUID.
        assert row.label == "quickbooks for alice@example.com"

    async def test_label_falls_back_when_no_subscription_email_exists(self) -> None:
        registry = _registry({"quickbooks"}, return_value={})
        with _integration_sweep([SimpleNamespace(id="user-1", user_id="google_123")], registry):
            observations = await hm._probe_integrations()

        assert all(o.user_label == "google_123" for o in observations)

    async def test_user_cap_is_enforced_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        users = [SimpleNamespace(id=f"user-{i}", user_id=f"u{i}") for i in range(5)]
        registry = _registry({"quickbooks"}, return_value={})
        with (
            _integration_sweep(users, registry),
            patch.object(hm.settings, "health_probe_max_users", 2),
            caplog.at_level("WARNING"),
        ):
            observations = await hm._probe_integrations()
        # One integration key plus one sweep-check key, for each of two users.
        assert len(observations) == 4
        assert len({o.user_id for o in observations}) == 2
        # Truncation is never silent.
        assert "unmonitored" in caplog.text

    async def test_one_users_failure_does_not_abort_the_sweep(self) -> None:
        users = [
            SimpleNamespace(id="user-1", user_id="alice"),
            SimpleNamespace(id="user-2", user_id="bob"),
        ]
        registry = _registry({"quickbooks"}, side_effect=[RuntimeError("boom"), {}])
        with _integration_sweep(users, registry):
            observations = await hm._probe_integrations()
        assert [o.key for o in observations] == [
            "integration_check:user-1",
            "integration_check:user-2",
            "integration:quickbooks:user-2",
        ]

    async def test_a_user_that_cannot_be_checked_is_reported_not_skipped(self) -> None:
        """Skipping silently is how a lapsed token goes unreported.

        The sweep used to drop a user whose ``auth_check`` raised. Their probe
        states then kept their last known status forever, so an integration that
        broke while the check was failing produced no transition and no email.
        """
        registry = _registry({"quickbooks"}, side_effect=RuntimeError("boom"))
        with _integration_sweep(
            [SimpleNamespace(id="user-1", user_id="alice")],
            registry,
            labels={"user-1": "alice@example.com"},
        ):
            observations = await hm._probe_integrations()

        assert len(observations) == 1
        check = observations[0]
        assert check.key == "integration_check:user-1"
        assert check.ok is False
        assert "boom" in check.detail
        assert check.label == "Integration checks for alice@example.com"
        # Not baseline-silent: a check that cannot run has no steady state that
        # is legitimately failing, so the first observation is already news.
        assert check.alert_on_first_observation is True

    async def test_a_timed_out_user_check_says_so(self) -> None:
        registry = _registry({"quickbooks"}, side_effect=TimeoutError())
        with (
            _integration_sweep([SimpleNamespace(id="user-1", user_id="alice")], registry),
            patch.object(hm.settings, "health_probe_timeout_seconds", 7),
        ):
            observations = await hm._probe_integrations()

        assert observations[0].ok is False
        assert "did not answer within 7s" in observations[0].detail

    async def test_labels_come_from_the_premium_subscription_row(
        self, db_session: Session, test_user: User
    ) -> None:
        # The email lives on the premium Subscription, so this is the one part
        # of the sweep OSS cannot provide.
        db_session.add(
            Subscription(user_id=test_user.id, role="user", email="alice@example.com", plan="free")
        )
        db_session.commit()

        assert (await hm._user_labels())[test_user.id] == "alice@example.com"

    async def test_a_label_lookup_failure_does_not_take_the_sweep_down(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Cosmetic data. Losing it degrades labels to the user id; it must never
        # cost the monitoring itself.
        with (
            patch.object(hm, "AsyncSessionLocal", side_effect=RuntimeError("no pool")),
            caplog.at_level("WARNING"),
        ):
            assert await hm._user_labels() == {}
        assert "subscription emails" in caplog.text

    async def test_a_successful_sweep_reports_the_check_as_healthy(self) -> None:
        # Needed for the recovery half: without an OK observation the check key
        # would sit DOWN forever after one bad tick.
        registry = _registry({"quickbooks"}, return_value={})
        with _integration_sweep([SimpleNamespace(id="user-1", user_id="alice")], registry):
            observations = await hm._probe_integrations()

        check = next(o for o in observations if o.key.startswith(hm._INTEGRATION_CHECK_PREFIX))
        assert check.ok is True
        assert check.integration == ""


class TestRunOnceAndEnablement:
    async def test_transitions_are_emailed(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with (
            patch.object(
                monitor, "_collect", new_callable=AsyncMock, return_value=[_obs("database", False)]
            ),
            patch.object(hm.settings, "health_failure_threshold", 1),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock) as send,
        ):
            transitions = await monitor.run_once()
        assert len(transitions) == 1
        send.assert_awaited_once()
        assert send.await_args is not None
        assert send.await_args.args[0] == "admin@example.com"

    async def test_no_transitions_sends_no_email(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with (
            patch.object(
                monitor, "_collect", new_callable=AsyncMock, return_value=[_obs("database", True)]
            ),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock) as send,
        ):
            assert await monitor.run_once() == []
        send.assert_not_awaited()

    def test_disabled_without_recipient(self, alerts_configured: None) -> None:
        with (
            patch.object(hm.settings, "alert_email", ""),
            patch.object(hm.settings, "admin_email", ""),
        ):
            assert not hm.is_enabled()

    def test_disabled_by_flag(self, alerts_configured: None) -> None:
        with patch.object(hm.settings, "health_monitor_enabled", False):
            assert not hm.is_enabled()

    def test_start_is_a_noop_when_disabled(self, monitor: hm.HealthMonitor) -> None:
        with patch.object(hm.settings, "smtp_host", ""):
            assert monitor.start() is False

    async def test_a_users_integration_breaking_is_emailed(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        """The whole point of the per-user sweep: hear about it without looking.

        A tenant's connection that worked and then stopped is the one integration
        failure that is genuinely news, and it has to reach the admin's inbox
        naming the tenant, not a UUID.
        """
        working = hm.Observation(
            key="integration:quickbooks:user-1",
            label="quickbooks for alice@example.com",
            ok=True,
            alert_on_first_observation=False,
            user_id="user-1",
            user_label="alice@example.com",
            integration="quickbooks",
        )
        lapsed = replace(working, ok=False, detail="QuickBooks is not connected.")
        with (
            patch.object(hm.settings, "health_failure_threshold", 1),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock) as send,
        ):
            with patch.object(monitor, "_collect", new_callable=AsyncMock, return_value=[working]):
                await monitor.run_once()
            send.assert_not_awaited()

            with patch.object(monitor, "_collect", new_callable=AsyncMock, return_value=[lapsed]):
                transitions = await monitor.run_once()

        send.assert_awaited_once()
        assert [t.status for t in transitions] == [hm.STATUS_DOWN]
        assert transitions[0].label == "quickbooks for alice@example.com"

    async def test_a_users_checks_going_dark_is_emailed(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        # A sweep that cannot answer freezes every integration under it, so it
        # alerts on its own rather than waiting for a transition that cannot
        # happen while the check is failing.
        check = hm.Observation(
            key="integration_check:user-1",
            label="Integration checks for alice@example.com",
            ok=False,
            detail="Check did not answer within 45s.",
            user_id="user-1",
            user_label="alice@example.com",
        )
        with (
            patch.object(hm.settings, "health_failure_threshold", 1),
            patch.object(monitor, "_collect", new_callable=AsyncMock, return_value=[check]),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock) as send,
        ):
            transitions = await monitor.run_once()

        send.assert_awaited_once()
        assert [t.status for t in transitions] == [hm.STATUS_DOWN]

    def test_snapshot_shape(self, monitor: hm.HealthMonitor, alerts_configured: None) -> None:
        monitor._apply([_obs("database", True, detail="")])
        entry = monitor.snapshot()["database"]
        assert set(entry) == {
            "label",
            "status",
            "detail",
            "consecutive_failures",
            "since",
            "last_checked",
            "never_connected",
            "user_id",
            "user_label",
            "integration",
        }
        assert entry["last_checked"] is not None
        # Infrastructure probes belong to no tenant, so the tab does not try to
        # group them by one.
        assert (entry["user_id"], entry["user_label"], entry["integration"]) == ("", "", "")

    def test_snapshot_carries_per_user_grouping(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        monitor._apply(
            [
                hm.Observation(
                    key="integration:quickbooks:user-1",
                    label="quickbooks for alice@example.com",
                    ok=True,
                    alert_on_first_observation=False,
                    user_id="user-1",
                    user_label="alice@example.com",
                    integration="quickbooks",
                )
            ]
        )
        entry = monitor.snapshot()["integration:quickbooks:user-1"]
        assert entry["user_id"] == "user-1"
        assert entry["user_label"] == "alice@example.com"
        assert entry["integration"] == "quickbooks"


class TestActivityHistory:
    """Backs the admin Monitoring tab, which needs more than the current state."""

    def test_transitions_are_recorded_newest_first(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with patch.object(hm.settings, "health_failure_threshold", 1):
            monitor._apply([_obs("database", False, detail="SELECT 1 failed")])
            monitor._apply([_obs("database", True)])

        history = monitor.history()
        assert [event["status"] for event in history] == [hm.STATUS_UP, hm.STATUS_DOWN]
        assert history[1]["detail"] == "SELECT 1 failed"
        assert history[0]["key"] == "database"

    def test_steady_state_does_not_fill_the_log(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        for _ in range(5):
            monitor._apply([_obs("database", True)])

        assert monitor.history() == []

    def test_repairs_are_recorded_alongside_transitions(self, monitor: hm.HealthMonitor) -> None:
        monitor.record_event(
            "bluebubbles", "BlueBubbles inbound webhook", hm.STATUS_REPAIRED, "fixed"
        )

        assert monitor.history()[0]["status"] == hm.STATUS_REPAIRED

    def test_history_is_bounded(self, monitor: hm.HealthMonitor) -> None:
        for i in range(hm._HISTORY_LIMIT + 25):
            monitor.record_event("database", "PostgreSQL", hm.STATUS_DOWN, f"failure {i}")

        assert len(monitor.history()) == hm._HISTORY_LIMIT

    async def test_run_once_stamps_the_last_run_time(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        assert monitor.last_run_at is None
        with patch.object(
            monitor, "_collect", new_callable=AsyncMock, return_value=[_obs("database", True)]
        ):
            await monitor.run_once()

        assert monitor.last_run_at is not None


class TestProbeTimeouts:
    """A probe that never returns is a failure, not a reason to stall the run.

    Probes call a residential Mac, an LLM provider and a scraping sidecar. None
    of them is obliged to answer, and an unbounded await on any one of them held
    the whole run open, which is what left the admin tab on "Running".
    """

    async def test_a_hanging_probe_becomes_a_down_observation(
        self, monitor: hm.HealthMonitor
    ) -> None:
        async def _hang() -> hm.Observation:
            await asyncio.sleep(30)
            raise AssertionError("probe should have been abandoned")

        probe = hm._InfraProbe("bluebubbles", _hang, "BlueBubbles bridge")
        with (
            patch.object(hm, "_MIN_PROBE_TIMEOUT", 1),
            patch.object(hm.settings, "health_probe_timeout_seconds", 1),
        ):
            observation = await monitor._run_probe(probe, None)

        assert observation.ok is False
        assert observation.key == "bluebubbles"
        assert observation.label == "BlueBubbles bridge"
        assert "did not answer within" in observation.detail

    async def test_a_hanging_probe_does_not_delay_the_others(
        self, monitor: hm.HealthMonitor
    ) -> None:
        async def _hang() -> hm.Observation:
            await asyncio.sleep(30)
            raise AssertionError("probe should have been abandoned")

        async def _fast() -> hm.Observation:
            return _obs("database", True)

        probes = [
            hm._InfraProbe("bluebubbles", _hang, "BlueBubbles bridge"),
            hm._InfraProbe("database", _fast, "PostgreSQL"),
        ]
        with (
            patch.object(monitor, "_infra_probes", return_value=probes),
            patch.object(hm, "_probe_integrations", new_callable=AsyncMock, return_value=[]),
            patch.object(hm, "_MIN_PROBE_TIMEOUT", 1),
            patch.object(hm.settings, "health_probe_timeout_seconds", 1),
        ):
            started = time.monotonic()
            observations = await monitor._collect()
            elapsed = time.monotonic() - started

        # Concurrent, so the run costs the slowest probe's ceiling, not the sum.
        assert elapsed < 5
        assert {o.key: o.ok for o in observations} == {"bluebubbles": False, "database": True}

    async def test_one_stuck_tenant_does_not_consume_the_sweep(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        users = [
            SimpleNamespace(id="user-1", user_id="alice"),
            SimpleNamespace(id="user-2", user_id="bob"),
        ]

        async def _check(ctx: object) -> dict[str, str]:
            if ctx.user.id == "user-1":  # type: ignore[attr-defined]
                await asyncio.sleep(30)
            return {}

        registry = MagicMock()
        registry.specialist_factory_names = {"quickbooks"}
        registry.get_unauthenticated_specialists = _check
        with (
            _integration_sweep(users, registry),
            patch.object(hm, "_MIN_PROBE_TIMEOUT", 1),
            patch.object(hm.settings, "health_probe_timeout_seconds", 1),
            caplog.at_level("WARNING"),
        ):
            observations = await hm._probe_integrations()

        # The stuck tenant is abandoned, and reported as unknown rather than
        # dropped; the rest of the sweep runs.
        assert [o.key for o in observations] == [
            "integration_check:user-1",
            "integration_check:user-2",
            "integration:quickbooks:user-2",
        ]
        assert observations[0].ok is False
        assert "did not answer within 1s" in caplog.text


class TestRunProgress:
    """The admin tab polls this. It is the only view into a run in flight."""

    async def test_steps_are_published_before_any_work_starts(
        self, monitor: hm.HealthMonitor
    ) -> None:
        async def _fast() -> hm.Observation:
            return _obs("database", True)

        with patch.object(
            monitor, "_infra_probes", return_value=[hm._InfraProbe("database", _fast, "PostgreSQL")]
        ):
            run = monitor._begin_run("manual")

        progress = monitor.run_progress()
        assert progress is not None
        assert progress["running"] is True
        assert [step.key for step in run.steps] == ["database", "integrations", "alert_email"]
        # Pending, not absent: a view that lists only finished work cannot tell
        # "slow" from "not started".
        assert {step.status for step in run.steps} == {hm.STEP_PENDING}

    async def test_each_step_records_its_outcome(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        async def _ok() -> hm.Observation:
            return _obs("database", True)

        async def _bad() -> hm.Observation:
            return _obs("supplier_pricing", False, detail="zero results")

        probes = [
            hm._InfraProbe("database", _ok, "PostgreSQL"),
            hm._InfraProbe("supplier_pricing", _bad, "Home Depot search"),
        ]
        with (
            patch.object(monitor, "_infra_probes", return_value=probes),
            patch.object(hm, "_probe_integrations", new_callable=AsyncMock, return_value=[]),
            patch.object(hm.settings, "health_failure_threshold", 1),
            patch.object(
                hm.email_service, "send_health_alert", new_callable=AsyncMock, return_value=True
            ),
        ):
            await monitor.run_once("manual")

        progress = monitor.run_progress()
        assert progress is not None
        steps = {step["key"]: step for step in _steps(progress)}
        assert progress["running"] is False
        assert steps["database"]["status"] == hm.STEP_OK
        assert steps["supplier_pricing"]["status"] == hm.STEP_FAILED
        assert steps["supplier_pricing"]["detail"] == "zero results"
        assert steps["supplier_pricing"]["elapsed_ms"] is not None
        assert steps["alert_email"]["status"] == hm.STEP_OK
        assert "supplier_pricing DOWN" in str(steps["alert_email"]["detail"])

    async def test_the_integration_sweep_reports_as_it_advances(
        self, monitor: hm.HealthMonitor
    ) -> None:
        seen: list[str] = []

        async def _sweep(
            on_progress: Callable[[str], None] | None = None,
        ) -> list[hm.Observation]:
            assert on_progress is not None
            on_progress("checking user 7 of 50")
            steps = _steps(monitor.run_progress())
            seen.append(str(next(s["detail"] for s in steps if s["key"] == _STEP_INTEGRATIONS_KEY)))
            return []

        with (
            patch.object(monitor, "_infra_probes", return_value=[]),
            patch.object(hm, "_probe_integrations", new=_sweep),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock),
        ):
            await monitor.run_once("manual")

        # The longest part of a run, so a manual run looks stalled without this.
        assert seen == ["checking user 7 of 50"]

    async def test_a_failed_send_is_reported_on_the_email_step(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        """A dead transport is the failure that hides every other failure."""
        with (
            patch.object(
                monitor, "_collect", new_callable=AsyncMock, return_value=[_obs("database", False)]
            ),
            patch.object(hm.settings, "health_failure_threshold", 1),
            patch.object(
                hm.email_service, "send_health_alert", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                hm.email_service,
                "transport_status",
                return_value=SimpleNamespace(last_error="Timed out after 10s talking to smtp:587"),
            ),
        ):
            await monitor.run_once("manual")

        email_step = next(s for s in _steps(monitor.run_progress()) if s["key"] == "alert_email")
        assert email_step["status"] == hm.STEP_FAILED
        assert "was NOT emailed" in str(email_step["detail"])
        assert "Timed out after 10s" in str(email_step["detail"])

    async def test_no_transitions_still_closes_the_email_step(
        self, monitor: hm.HealthMonitor, alerts_configured: None
    ) -> None:
        with (
            patch.object(
                monitor, "_collect", new_callable=AsyncMock, return_value=[_obs("database", True)]
            ),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock) as send,
        ):
            await monitor.run_once("scheduled")

        email_step = next(s for s in _steps(monitor.run_progress()) if s["key"] == "alert_email")
        assert email_step["status"] == hm.STEP_OK
        assert email_step["detail"] == "No status change to report"
        send.assert_not_awaited()

    async def test_a_run_that_raises_is_visible_rather_than_stuck_on_running(
        self, monitor: hm.HealthMonitor
    ) -> None:
        with (
            patch.object(
                monitor, "_collect", new_callable=AsyncMock, side_effect=RuntimeError("boom")
            ),
            pytest.raises(RuntimeError),
        ):
            await monitor.run_once("manual")

        progress = monitor.run_progress()
        assert progress is not None
        assert progress["running"] is False
        assert "boom" in str(progress["error"])
        # Steps that never reported must not read as probes still in flight.
        assert all(step["status"] != hm.STEP_RUNNING for step in _steps(progress))


class TestBackgroundRun:
    """The endpoint starts a run rather than awaiting it."""

    async def test_start_run_returns_and_the_work_happens_after(
        self, monitor: hm.HealthMonitor
    ) -> None:
        gate = asyncio.Event()

        async def _collect(run: object = None) -> list[hm.Observation]:
            await gate.wait()
            return [_obs("database", True)]

        with (
            patch.object(monitor, "_collect", new=_collect),
            patch.object(hm.email_service, "send_health_alert", new_callable=AsyncMock),
        ):
            assert monitor.start_run("manual") is True
            await asyncio.sleep(0)  # let the task reach the gate
            assert monitor.is_running is True
            # A second request must not start a competing pass: two would double
            # every outbound call and race on the transition bookkeeping.
            assert monitor.start_run("manual") is False

            gate.set()
            assert monitor._run_task is not None
            await monitor._run_task

        assert monitor.is_running is False
        assert monitor.last_run_at is not None

    async def test_a_raising_run_does_not_leave_the_lock_held(
        self, monitor: hm.HealthMonitor
    ) -> None:
        with patch.object(
            monitor, "_collect", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            assert monitor.start_run("manual") is True
            assert monitor._run_task is not None
            await monitor._run_task

            # A held lock would wedge every later run in the process.
            assert monitor.is_running is False
            assert monitor.start_run("manual") is True
            await monitor._run_task
