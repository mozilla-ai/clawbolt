"""Proactive dependency health monitoring with transition-based email alerts.

Proactive half of the monitoring stack. ``admin_alerts`` covers errors the app
logged when a user hit them; this module finds breakage nobody has hit yet, and
specifically the failures that never raise:

- A BlueBubbles bridge on a sleeping Mac accepts nothing and logs nothing.
- A supplier sidecar whose browser crashes can remain reachable while unable to
  answer its local health round-trip.
- An expired OAuth token surfaces only when the agent next reaches for that
  tool, which may be days after it lapsed.

Design:

- **Alerts fire on transitions, not on state.** A probe failing for six hours
  is two emails (down, then recovered), not 72. Steady-state DOWN is
  deliberately silent: the first email already said so.
- **Consecutive-failure threshold.** ``health_failure_threshold`` probes must
  fail in a row before declaring DOWN. One timed-out request to a residential
  BlueBubbles host is noise.
- **Baseline seeding for per-user integrations.** An integration a user never
  connected reports "not authenticated" forever; that is a user choice, not an
  outage. Those keys establish their baseline silently and alert only on a
  genuine UP -> DOWN transition, i.e. a token that worked and then stopped.
  Infrastructure probes have no such ambiguity and alert on first observation.
- **The per-user sweep reports on itself.** A user whose ``auth_check`` times
  out or raises gets an ``integration_check:<user_id>`` failure rather than
  being skipped in silence. Skipping left that tenant's integrations frozen at
  their last known status, so one that broke while the check was failing
  produced no transition and no email. That key is not baseline-silent: a check
  that cannot run has no legitimate steady state.
- **Unconfigured dependencies are not probed.** No BlueBubbles URL means no
  BlueBubbles probe, rather than a permanently-red check.
- **Probes never raise into the loop.** An exception inside a probe is itself a
  failure signal, recorded as DOWN with the exception text as detail.
- **A repair reports itself.** The BlueBubbles inbound webhook is re-registered
  when it is found missing. Because that resolves the failure before the
  consecutive-failure threshold is met, no DOWN transition would ever fire, so
  the repair sends its own email and writes its own activity-log entry.
- **Every probe is time-boxed, and a run reports its progress.** Probes call a
  residential Mac, an LLM provider, and a scraping sidecar; none of them is
  obliged to answer. Without a per-probe ceiling one wedged socket stalls the
  whole run, and an on-demand run from the admin tab sits on "Running" with
  nothing to show. A run publishes per-step state (pending, running, ok,
  failed) so the caller can watch it advance and see which dependency is slow.

Reuse: the database probe calls the OSS ``/health`` handler directly rather than
re-implementing ``SELECT 1``, and the BlueBubbles probe reads the health result
the OSS channel's own ``_health_loop`` already maintains instead of adding a
second poller against the same host.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from any_llm import amessages
from sqlalchemy import select

from backend.app.agent.dto import UserData
from backend.app.agent.tools.registry import ToolContext, default_registry
from backend.app.agent.user_db import get_user_store
from backend.app.channels import get_manager
from backend.app.channels.bluebubbles import (
    BlueBubblesChannel,
    build_webhook_url,
    describe_send_readiness,
    register_bluebubbles_webhook,
    verify_webhook_registration,
)
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal
from backend.app.integrations.supplier_pricing.sidecar_client import SidecarSupplier
from backend.app.models import Subscription, User
from backend.app.routers.health import health_check
from backend.app.services import email_service

logger = logging.getLogger(__name__)

STATUS_UNKNOWN = "unknown"
STATUS_UP = "up"
STATUS_DOWN = "down"
# Not a probe state: a one-off activity-log entry for something the monitor
# found broken and fixed itself.
STATUS_REPAIRED = "repaired"

# Per-step states for one run of the probe set. Distinct from probe status on
# purpose: a step describes whether the *check* completed, which is what an
# operator watching a manual run needs, while probe status describes the
# dependency and carries the consecutive-failure history.
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_OK = "ok"
STEP_FAILED = "failed"

# Step keys that are not infrastructure probes.
_STEP_INTEGRATIONS = "integrations"
_STEP_EMAIL = "alert_email"

# Floor on the per-probe ceiling, so a mistyped HEALTH_PROBE_TIMEOUT_SECONDS
# cannot turn every probe into a timeout. Tests patch it to keep the suite fast.
_MIN_PROBE_TIMEOUT = 5


def _probe_timeout() -> int:
    return max(_MIN_PROBE_TIMEOUT, settings.health_probe_timeout_seconds)


# Bounded in-process activity log behind the admin Monitoring tab. Deliberately
# not persisted: it is a live operations view, and a redeploy starting from an
# empty log is acceptable where a migration and a write on every tick are not.
_HISTORY_LIMIT = 200

# The default APP_BASE_URL. The lifespan skips webhook registration for it,
# and reads this same constant, so the check and the registration cannot
# disagree about what counts as a deployed URL.
LOCAL_BASE_URL = "http://localhost:8000"

# Probe keys for per-user integration checks are namespaced so the admin status
# view can separate infrastructure from tenant-specific breakage.
_INTEGRATION_PREFIX = "integration:"

# One key per user for the sweep itself, distinct from the per-integration keys
# it produces. A user whose auth_check times out or raises used to be skipped
# outright: no observation, no transition, no email, and their last known
# statuses silently went stale. That is the exact shape of "an integration
# started erroring and nobody heard", so the sweep now reports on itself.
_INTEGRATION_CHECK_PREFIX = "integration_check:"


def _recipient() -> str:
    return settings.alert_email or settings.admin_email


def is_enabled() -> bool:
    """True when the monitor is on and alerts can actually be delivered."""
    return bool(
        settings.health_monitor_enabled
        and settings.smtp_host
        and settings.smtp_from_email
        and _recipient()
    )


@dataclass(frozen=True)
class Observation:
    """Outcome of one probe on one tick."""

    key: str
    label: str
    ok: bool
    detail: str = ""
    # False for checks whose "failing" baseline is a legitimate steady state
    # (an integration the user simply never connected).
    alert_on_first_observation: bool = True
    # Grouping metadata for per-user checks. Carried as fields rather than left
    # for the admin tab to parse back out of the key: the key is an internal
    # identifier, and a UI that has to split it on ":" breaks the moment a
    # factory name or a user id contains one.
    user_id: str = ""
    user_label: str = ""
    integration: str = ""


@dataclass(frozen=True)
class HealthTransition:
    """A status change worth emailing about."""

    key: str
    label: str
    status: str
    detail: str
    since: datetime
    consecutive_failures: int


@dataclass(frozen=True)
class _InfraProbe:
    """A named infrastructure probe.

    The name is carried explicitly rather than read off ``__name__`` so a probe
    can be a closure or a partial without the fallback key becoming meaningless.

    ``label`` is what a run's progress calls this step before the probe has
    returned anything. The probe's own Observation carries the authoritative
    label (the LLM one names the live provider and model), but that arrives only
    at the end, and a progress view that cannot name a step until it finishes is
    not a progress view. Defaults to the name.
    """

    name: str
    run: Callable[[], Awaitable[Observation]]
    label: str = ""


@dataclass
class _RunStep:
    """One unit of work inside a probe run, as the admin tab sees it."""

    key: str
    label: str
    status: str = STEP_PENDING
    detail: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def start(self) -> None:
        self.status = STEP_RUNNING
        self.started_at = datetime.now(UTC)

    def finish(self, *, ok: bool, detail: str = "") -> None:
        self.status = STEP_OK if ok else STEP_FAILED
        self.detail = detail
        self.finished_at = datetime.now(UTC)

    def as_dict(self) -> dict[str, object]:
        elapsed_ms: int | None = None
        if self.started_at is not None:
            end = self.finished_at or datetime.now(UTC)
            elapsed_ms = int((end - self.started_at).total_seconds() * 1000)
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            # Computed live for a running step, so a slow dependency is visibly
            # slow while it is still being waited on rather than only in
            # hindsight.
            "elapsed_ms": elapsed_ms,
        }


@dataclass
class _Run:
    """Progress of a single pass over every probe.

    Only the most recent run is kept. The durable history of *outcomes* is the
    activity log and the alert emails; this is the live view of one pass, and it
    outlives the pass so the tab can still show what the last run did.
    """

    trigger: str
    started_at: datetime
    steps: list[_RunStep]
    finished_at: datetime | None = None
    error: str = ""

    def step(self, key: str) -> _RunStep | None:
        for step in self.steps:
            if step.key == key:
                return step
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "running": self.finished_at is None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "steps": [step.as_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class HealthEvent:
    """One entry in the admin-visible activity log.

    Covers both status transitions and self-repairs. A repair is not a status,
    so it would otherwise leave no trace anywhere an admin can see.
    """

    at: datetime
    key: str
    label: str
    status: str
    detail: str


@dataclass
class _ProbeState:
    label: str
    status: str = STATUS_UNKNOWN
    consecutive_failures: int = 0
    detail: str = ""
    since: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_checked: datetime | None = None
    # Whether this probe has ever reported healthy, and whether its failing
    # baseline is a legitimate steady state. Together they separate "broken"
    # from "the user never connected this", which share the status DOWN but
    # are not the same news. Without the distinction every unconnected
    # integration counts as an outage in the admin view: 8 specialist
    # integrations across 50 users is 400 red rows on a healthy deployment.
    ever_up: bool = False
    baseline_silent: bool = False
    # Grouping metadata, empty for infrastructure probes. See ``Observation``.
    user_id: str = ""
    user_label: str = ""
    integration: str = ""

    @property
    def never_connected(self) -> bool:
        return self.status == STATUS_DOWN and self.baseline_silent and not self.ever_up


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


async def _probe_database() -> Observation:
    """Reuse the OSS health handler so there is one definition of "DB is up"."""
    response = await health_check()
    ok = response.database == "ok"
    return Observation(
        key="database",
        label="PostgreSQL",
        ok=ok,
        detail="" if ok else "SELECT 1 failed; see the application log for the exception",
    )


async def _probe_llm() -> Observation:
    """Single-token completion against the primary provider/model.

    Catches a revoked API key, a model the provider retired, and provider-side
    outages, none of which surface until a user sends a message.

    TODO(oss): OSS ``_verify_llm_settings`` runs the same ping as a startup
    gate that raises. Extracting a shared non-raising ``probe_llm(provider,
    model)`` helper into OSS would let both callers share it, per the
    no-duplication rule in CLAUDE.md.
    """
    label = f"LLM ({settings.llm_provider}/{settings.llm_model})"
    try:
        await amessages(
            model=settings.llm_model,
            provider=settings.llm_provider,
            api_base=settings.llm_api_base,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception as exc:
        return Observation(key="llm", label=label, ok=False, detail=f"{type(exc).__name__}: {exc}")
    return Observation(key="llm", label=label, ok=True)


# Timestamp of the last webhook-repair email, so a webhook that keeps
# vanishing does not email on every tick.
_repair_notice_sent_at: datetime | None = None

# Consecutive repairs attempted without the check ever passing afterwards.
_repair_attempts: int = 0

# Ceiling on those attempts. Registration is delete-then-POST against the
# operator's Mac, so a repair that never takes hold is not a harmless retry: it
# reopens a window with no webhook registered on every tick, forever, while the
# email cooldown keeps the operator from hearing about it more than once. Past
# this many attempts the probe stops writing and simply reports DOWN, which
# escalates through the normal transition alert instead.
_MAX_CONSECUTIVE_REPAIRS = 3


async def _repair_inbound_webhook(expected_url: str, problem: str) -> str:
    """Re-register the inbound webhook and email the operator about it.

    The repair needs its own email rather than riding the transition
    machinery. That machinery requires ``health_failure_threshold``
    consecutive failures before it reports DOWN, and a repair that lands on
    the first failing tick means the threshold is never reached: inbound
    iMessage would break, silently fix itself, and nobody would learn that it
    had been down or that it keeps happening.

    Returns a description of what happened, for the probe detail.
    """
    global _repair_notice_sent_at, _repair_attempts

    endpoint = expected_url.split("?")[0]
    _repair_attempts += 1
    repaired = await register_bluebubbles_webhook(settings.bluebubbles_server_url, expected_url)
    if repaired:
        outcome = f"Re-registered {endpoint}. Inbound iMessage should resume immediately."
    else:
        outcome = f"Re-registration of {endpoint} failed. Inbound iMessage is still down."
    # Warning rather than error on purpose: an ERROR record would also be
    # picked up by the layer-1 alert handler and emailed a second time. The
    # dedicated notice below is the alert, and an unsuccessful repair keeps
    # failing the probe until the transition alert fires on its own.
    logger.warning("BlueBubbles inbound webhook: %s %s", problem, outcome)

    health_monitor.record_event(
        key="bluebubbles",
        label="BlueBubbles inbound webhook",
        status=STATUS_REPAIRED if repaired else STATUS_DOWN,
        detail=f"{problem} {outcome}",
    )

    now = datetime.now(UTC)
    cooldown = timedelta(minutes=max(1, settings.alert_dedupe_minutes))
    if _repair_notice_sent_at is None or now - _repair_notice_sent_at >= cooldown:
        sent = await email_service.send_repair_notice(
            _recipient(),
            label="BlueBubbles inbound webhook",
            problem=problem,
            outcome=outcome,
        )
        if sent:
            _repair_notice_sent_at = now
    return outcome


async def _check_inbound_webhook() -> str:
    """Verify the BlueBubbles server will still deliver inbound messages.

    Returns ``""`` when inbound is fine, otherwise the problem description.

    This is the check with no other coverage. Registration is attempted once
    per deploy, in a background task; if the Mac is asleep at that moment the
    attempt fails, is never retried, and the bridge later comes back online
    with every reachability signal green while inbound stays dead. A changed
    ``APP_BASE_URL`` produces the same silence.
    """
    global _repair_attempts

    base = settings.app_base_url.rstrip("/")
    if not base or base == LOCAL_BASE_URL:
        # Matches the lifespan, which does not register a webhook for the
        # local default. Reporting it as missing would be a permanent red
        # light in development.
        return ""

    if health_monitor.last_run_at is None:
        # First tick of the process. The lifespan registers this webhook in a
        # background task, and that path deletes the previous deploy's
        # registration before POSTing the new one, so a check landing inside
        # that window sees "no webhooks registered" and would repair and email
        # about a deploy where nothing was ever wrong.
        return ""

    expected_url = build_webhook_url(base)
    check = await verify_webhook_registration(settings.bluebubbles_server_url, expected_url)
    if check.ok:
        _repair_attempts = 0
        return ""
    if not check.listed:
        # We could not ask, so we do not know it is missing. Repairing here
        # would write to the operator's Mac on the strength of a guess.
        return f"Inbound delivery unverified: {check.detail}"
    if _repair_attempts >= _MAX_CONSECUTIVE_REPAIRS:
        # Re-registering is not sticking. Stop writing to the operator's Mac
        # and let this fail as an ordinary DOWN, which the transition alert
        # reports on its own schedule.
        return (
            f"{check.detail}. Re-registered {_repair_attempts} times without it holding; "
            "no further automatic attempts. This needs manual attention."
        )
    return await _repair_inbound_webhook(expected_url, check.detail)


async def _probe_bluebubbles() -> Observation:
    """Check the bridge answers, can still send, and will still deliver inbound.

    Reuses the health result the OSS channel's own loop maintains rather than
    adding a second poller against the operator's Mac. That loop runs every
    ``bluebubbles_health_check_interval_seconds`` and owns the state; a second
    poller would double the load and could disagree with the dashboard.
    """
    key, label = "bluebubbles", "BlueBubbles bridge"
    channel = get_manager().channels.get("bluebubbles")
    if not isinstance(channel, BlueBubblesChannel):
        return Observation(
            key=key,
            label=label,
            ok=False,
            detail="Channel is configured but not registered with the channel manager",
        )

    # When the OSS loop is disabled its stored result is frozen at boot, which
    # would make this probe report hours-old news forever. Probe directly then.
    health = channel.last_health
    if health is None or settings.bluebubbles_health_check_interval_seconds <= 0:
        health = await channel.check_health()

    if not health.ok:
        return Observation(
            key=key,
            label=label,
            ok=False,
            detail=health.detail or "iMessage bridge is not answering /api/v1/server/info",
        )

    send_problem = describe_send_readiness(health)
    if send_problem:
        return Observation(
            key=key,
            label=label,
            ok=False,
            detail=f"Bridge is reachable but cannot send: {send_problem}",
        )

    inbound_problem = await _check_inbound_webhook()
    if inbound_problem:
        return Observation(key=key, label=label, ok=False, detail=inbound_problem)

    version = f" (server {health.server_version})" if health.server_version else ""
    return Observation(
        key=key, label=label, ok=True, detail=f"reachable, sending, inbound{version}"
    )


def _supplier_sidecar(site: str, name: str, display_name: str) -> SidecarSupplier:
    return SidecarSupplier(
        settings.home_depot_sidecar_url,
        site=site,
        name=name,
        display_name=display_name,
        token=settings.home_depot_sidecar_token,
    )


async def _probe_supplier_sidecar() -> Observation:
    """Check that the browser sidecar is alive without visiting either retailer.

    Home Depot and Lowe's both flag predictable automated searches. A health
    probe must therefore stop at the sidecar's local browser round-trip. Actual
    retailer searches remain user-driven, where their failures are captured by
    the normal application error alerts instead of becoming background traffic.
    """
    supplier = _supplier_sidecar("home_depot", "homedepot", "Retail search")
    try:
        healthy = await supplier.healthy()
    except Exception as exc:
        return Observation(
            key="supplier_sidecar",
            label="Retail search sidecar",
            ok=False,
            detail=f"Health check raised {type(exc).__name__}: {exc}",
        )
    if not healthy:
        return Observation(
            key="supplier_sidecar",
            label="Retail search sidecar",
            ok=False,
            detail="Browser is unavailable or warming. No retailer search was sent.",
        )
    return Observation(
        key="supplier_sidecar",
        label="Retail search sidecar",
        ok=True,
        detail="Browser responds. Background monitoring does not send retailer searches.",
    )


async def _user_labels() -> dict[str, str]:
    """Map ``users.id`` to the address the admin knows that tenant by.

    A per-user view keyed on a bare UUID answers "something is broken" without
    answering "for whom", which is the question that decides what to do about
    it. The email lives on the ``Subscription`` row.

    Best effort: a failure here degrades labels to the user id, and must not
    take the sweep down with it.
    """
    try:
        db = AsyncSessionLocal()
        try:
            rows = (await db.execute(select(Subscription.user_id, Subscription.email))).all()
        finally:
            await db.close()
    except Exception as exc:
        logger.warning("Could not load subscription emails for integration labels: %s", exc)
        return {}
    return {user_id: email for user_id, email in rows if email}


async def _probe_integrations(
    on_progress: Callable[[str], None] | None = None,
) -> list[Observation]:
    """Per-user OAuth integration health via each factory's ``auth_check``.

    Catches the case where a token that used to work stopped working: expired
    refresh token, revoked grant, disconnected app. Emits one key per
    (user, integration) so a single tenant's dead QuickBooks token does not read
    as a platform outage, plus one ``integration_check:<user_id>`` key per user
    for whether the sweep could answer at all.

    Baseline-silent by design: a never-connected integration reports the same
    "not authenticated" reason as a lapsed one, so the per-integration keys only
    alert on a genuine UP -> DOWN transition. The per-user check key is not
    baseline-silent: a check that cannot run has no legitimate steady state.

    ``on_progress`` receives a human-readable count as the sweep advances. This
    is the longest part of a run (one auth_check per specialist factory per
    user), so without it a manual run looks stalled for the whole sweep.
    """
    store = get_user_store()
    users = await store.list_all_async()
    cap = max(1, settings.health_probe_max_users)
    if len(users) > cap:
        logger.warning(
            "Integration health probe checking %d of %d users (HEALTH_PROBE_MAX_USERS=%d); "
            "the remainder are unmonitored this tick",
            cap,
            len(users),
            cap,
        )
        users = users[:cap]

    labels = await _user_labels()

    def _label(user: UserData) -> str:
        return labels.get(user.id) or user.user_id or user.id

    observations: list[Observation] = []
    per_user_timeout = _probe_timeout()
    for index, user in enumerate(users, start=1):
        if on_progress is not None:
            on_progress(f"checking user {index} of {len(users)}")
        user_label = _label(user)
        check_key = f"{_INTEGRATION_CHECK_PREFIX}{user.id}"
        check_label = f"Integration checks for {user_label}"
        ctx = ToolContext(user=User(id=user.id, user_id=user.user_id))
        try:
            unauthenticated = await asyncio.wait_for(
                default_registry.get_unauthenticated_specialists(ctx),
                timeout=per_user_timeout,
            )
        except Exception as exc:
            # One tenant's stuck or exploding auth_check must not consume the
            # whole run, so the sweep moves on. It does not move on silently:
            # without this observation the user's integrations keep reporting
            # their last known status forever, so an integration that broke
            # while the check was failing would never produce a transition and
            # never be emailed.
            detail = (
                f"Check did not answer within {per_user_timeout}s"
                if isinstance(exc, TimeoutError)
                else f"Check failed: {type(exc).__name__}: {exc}"
            )
            logger.warning("Integration auth check for user %s: %s", user.id, detail)
            observations.append(
                Observation(
                    key=check_key,
                    label=check_label,
                    ok=False,
                    detail=f"{detail}. This user's integration status is unknown.",
                    user_id=user.id,
                    user_label=user_label,
                )
            )
            continue

        observations.append(
            Observation(
                key=check_key,
                label=check_label,
                ok=True,
                user_id=user.id,
                user_label=user_label,
            )
        )
        for name in sorted(default_registry.specialist_factory_names):
            reason = unauthenticated.get(name)
            observations.append(
                Observation(
                    key=f"{_INTEGRATION_PREFIX}{name}:{user.id}",
                    label=f"{name} for {user_label}",
                    ok=reason is None,
                    detail=reason or "",
                    alert_on_first_observation=False,
                    user_id=user.id,
                    user_label=user_label,
                    integration=name,
                )
            )
    return observations


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class HealthMonitor:
    """Runs probes on a timer and emails the operator on status transitions."""

    def __init__(self) -> None:
        self._states: dict[str, _ProbeState] = {}
        self._task: asyncio.Task[None] | None = None
        self._history: deque[HealthEvent] = deque(maxlen=_HISTORY_LIMIT)
        self._last_run_at: datetime | None = None
        self._run: _Run | None = None
        self._run_task: asyncio.Task[None] | None = None
        # Serializes the timer tick against an operator's on-demand run. Two
        # concurrent passes would double every outbound call and race on the
        # transition bookkeeping, so one waits for the other.
        self._run_lock = asyncio.Lock()

    def record_event(self, key: str, label: str, status: str, detail: str) -> None:
        """Append to the activity log shown in the admin Monitoring tab."""
        self._history.append(
            HealthEvent(at=datetime.now(UTC), key=key, label=label, status=status, detail=detail)
        )

    def _infra_probes(self) -> list[_InfraProbe]:
        """Assemble the infrastructure probes that current config supports."""
        probes = [_InfraProbe("database", _probe_database, "PostgreSQL")]
        if settings.health_probe_llm and settings.llm_model:
            probes.append(
                _InfraProbe(
                    "llm", _probe_llm, f"LLM ({settings.llm_provider}/{settings.llm_model})"
                )
            )
        if settings.bluebubbles_server_url and settings.bluebubbles_password:
            probes.append(_InfraProbe("bluebubbles", _probe_bluebubbles, "BlueBubbles bridge"))
        if settings.home_depot_sidecar_url:
            probes.append(
                _InfraProbe("supplier_sidecar", _probe_supplier_sidecar, "Retail search sidecar")
            )
        return probes

    async def _run_probe(self, probe: _InfraProbe, step: _RunStep | None) -> Observation:
        """Run one probe under a timeout, converting every outcome into a result.

        A probe that raises is a failure signal, and so is a probe that never
        returns: an unbounded await on a residential Mac or a wedged sidecar
        would hold the entire run open. Both are folded into a DOWN observation
        so the transition machinery treats them like any other failure.
        """
        label = probe.label or probe.name
        timeout = _probe_timeout()
        if step is not None:
            step.start()
        try:
            observation = await asyncio.wait_for(probe.run(), timeout=timeout)
        except TimeoutError:
            observation = Observation(
                key=probe.name,
                label=label,
                ok=False,
                detail=f"Probe did not answer within {timeout}s and was abandoned",
            )
        except Exception as exc:
            observation = Observation(
                key=probe.name, label=label, ok=False, detail=f"{type(exc).__name__}: {exc}"
            )
        if step is not None:
            step.label = observation.label or label
            step.finish(ok=observation.ok, detail=observation.detail)
        return observation

    async def _collect(self, run: _Run | None = None) -> list[Observation]:
        """Run every probe concurrently, converting raises into DOWN results."""
        probes = self._infra_probes()
        observations = list(
            await asyncio.gather(
                *(self._run_probe(probe, run.step(probe.name) if run else None) for probe in probes)
            )
        )
        step = run.step(_STEP_INTEGRATIONS) if run else None

        def _note(detail: str) -> None:
            if step is not None:
                step.detail = detail

        if step is not None:
            step.start()
        try:
            integrations = await _probe_integrations(on_progress=_note if step else None)
        except Exception as exc:
            logger.exception("Integration health probe sweep failed")
            if step is not None:
                step.finish(ok=False, detail=f"Sweep failed: {type(exc).__name__}: {exc}")
        else:
            observations.extend(integrations)
            if step is not None:
                users = {obs.user_id for obs in integrations if obs.user_id}
                unreachable = sum(
                    1
                    for obs in integrations
                    if not obs.ok and obs.key.startswith(_INTEGRATION_CHECK_PREFIX)
                )
                failing = sum(
                    1
                    for obs in integrations
                    if not obs.ok and obs.key.startswith(_INTEGRATION_PREFIX)
                )
                detail = (
                    f"{len(users)} user{'' if len(users) == 1 else 's'}, "
                    f"{failing} integration{'' if failing == 1 else 's'} not authenticated"
                )
                if unreachable:
                    detail += (
                        f", {unreachable} user{'' if unreachable == 1 else 's'} "
                        "could not be checked"
                    )
                step.finish(ok=True, detail=detail)

        return observations

    def _apply(self, observations: list[Observation]) -> list[HealthTransition]:
        """Fold observations into probe state, returning the alertable changes."""
        threshold = max(1, settings.health_failure_threshold)
        transitions: list[HealthTransition] = []
        now = datetime.now(UTC)

        for obs in observations:
            state = self._states.get(obs.key)
            if state is None:
                state = _ProbeState(label=obs.label)
                self._states[obs.key] = state
            state.label = obs.label
            state.detail = obs.detail
            state.last_checked = now
            state.baseline_silent = not obs.alert_on_first_observation
            state.user_id = obs.user_id
            state.user_label = obs.user_label
            state.integration = obs.integration
            first_observation = state.status == STATUS_UNKNOWN

            if obs.ok:
                recovered = state.status == STATUS_DOWN
                state.consecutive_failures = 0
                state.ever_up = True
                if state.status != STATUS_UP:
                    state.status = STATUS_UP
                    state.since = now
                if recovered:
                    transitions.append(
                        HealthTransition(
                            key=obs.key,
                            label=obs.label,
                            status=STATUS_UP,
                            detail=obs.detail,
                            since=now,
                            consecutive_failures=0,
                        )
                    )
                continue

            state.consecutive_failures += 1
            if state.status == STATUS_DOWN:
                # Already reported. Steady-state DOWN stays silent.
                continue
            if state.consecutive_failures < threshold:
                continue
            if first_observation and not obs.alert_on_first_observation:
                # Baseline seeding: never-connected integrations are not news.
                state.status = STATUS_DOWN
                state.since = now
                continue
            state.status = STATUS_DOWN
            state.since = now
            transitions.append(
                HealthTransition(
                    key=obs.key,
                    label=obs.label,
                    status=STATUS_DOWN,
                    detail=obs.detail,
                    since=now,
                    consecutive_failures=state.consecutive_failures,
                )
            )

        for transition in transitions:
            self._history.append(
                HealthEvent(
                    at=transition.since,
                    key=transition.key,
                    label=transition.label,
                    status=transition.status,
                    detail=transition.detail,
                )
            )
        return transitions

    def _begin_run(self, trigger: str) -> _Run:
        """Publish the step list before any work starts.

        Steps are declared up front, pending, so the caller sees what a run
        consists of from the first poll. A progress view that only lists work
        already finished cannot distinguish "slow" from "not started".
        """
        steps = [_RunStep(key=p.name, label=p.label or p.name) for p in self._infra_probes()]
        steps.append(_RunStep(key=_STEP_INTEGRATIONS, label="Per-user integrations"))
        steps.append(_RunStep(key=_STEP_EMAIL, label="Email status changes"))
        run = _Run(trigger=trigger, started_at=datetime.now(UTC), steps=steps)
        self._run = run
        return run

    async def run_once(
        self, trigger: str = "scheduled", run: _Run | None = None
    ) -> list[HealthTransition]:
        """Run one full tick: probe, fold, email any transitions.

        Holds ``_run_lock`` so the timer tick and an operator's on-demand run
        cannot overlap. ``run`` is supplied by ``start_run``, which publishes the
        step list before the task is scheduled so the HTTP response that started
        the run already carries it.
        """
        async with self._run_lock:
            if run is None:
                run = self._begin_run(trigger)
            try:
                observations = await self._collect(run)
                transitions = self._apply(observations)
                self._last_run_at = datetime.now(UTC)
                await self._email_transitions(transitions, run)
                return transitions
            except Exception as exc:
                run.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                run.finished_at = datetime.now(UTC)
                for step in run.steps:
                    if step.status in (STEP_PENDING, STEP_RUNNING):
                        # The run ended without this step reporting, which only
                        # happens when the run itself failed. Leaving it
                        # "running" forever would read as a hung probe.
                        step.finish(ok=False, detail="Did not complete: the run ended first")

    async def _email_transitions(self, transitions: list[HealthTransition], run: _Run) -> None:
        """Send the transition email, recording the outcome as a run step.

        The send is a step rather than a silent tail call because a dead
        transport is the failure that hides every other failure: probes go red,
        nothing arrives, and the only trace is a log line nobody is reading.
        """
        step = run.step(_STEP_EMAIL)
        if step is not None:
            step.start()
        if not transitions:
            if step is not None:
                step.finish(ok=True, detail="No status change to report")
            return
        summary = ", ".join(f"{t.label} {t.status.upper()}" for t in transitions)
        sent = await email_service.send_health_alert(_recipient(), transitions)
        if step is None:
            return
        if sent:
            step.finish(ok=True, detail=f"Emailed: {summary}")
        else:
            status = email_service.transport_status()
            step.finish(
                ok=False,
                detail=(
                    f"{summary} was NOT emailed. {status.last_error or 'Email is not configured.'}"
                ),
            )

    def start_run(self, trigger: str = "manual") -> bool:
        """Kick off a run in the background. False if one is already in flight.

        The admin endpoint returns immediately instead of awaiting the run: a
        full pass calls an LLM provider, a residential Mac, a scraping sidecar
        and one auth_check per specialist per user, which is minutes of
        wall-clock in the worst case. Holding an HTTP request open for that is
        what made the tab appear to hang with nothing to show.
        """
        if self.is_running:
            return False
        # Published here, not inside the task: the task does not run until this
        # coroutine yields, and the response that starts a run should already be
        # able to show what the run consists of.
        run = self._begin_run(trigger)
        self._run_task = asyncio.create_task(
            self._run_in_background(trigger, run), name="health-run"
        )
        return True

    async def _run_in_background(self, trigger: str, run: _Run) -> None:
        try:
            await self.run_once(trigger, run=run)
        except Exception as exc:
            # run_once already recorded the error on the run, so the tab shows
            # it; this keeps the task from dying with an unretrieved exception.
            logger.exception("Health probe run (%s) failed: %s", trigger, exc)

    @property
    def is_running(self) -> bool:
        return self._run_lock.locked() or (self._run_task is not None and not self._run_task.done())

    def run_progress(self) -> dict[str, object] | None:
        """Step-by-step state of the current or most recent run."""
        return self._run.as_dict() if self._run is not None else None

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Current probe state, for the admin monitoring endpoint."""
        return {
            key: {
                "label": state.label,
                "status": state.status,
                "detail": state.detail,
                "consecutive_failures": state.consecutive_failures,
                "since": state.since.isoformat(),
                "last_checked": state.last_checked.isoformat() if state.last_checked else None,
                # DOWN, but only because it was never connected in the first
                # place. The admin view shows these separately from breakage.
                "never_connected": state.never_connected,
                # Empty for infrastructure probes. The admin tab groups per-user
                # checks on ``user_id`` and titles each group with
                # ``user_label``; ``integration`` is empty on the per-user sweep
                # check, which covers the whole user rather than one integration.
                "user_id": state.user_id,
                "user_label": state.user_label,
                "integration": state.integration,
            }
            for key, state in sorted(self._states.items())
        }

    def history(self, limit: int = _HISTORY_LIMIT) -> list[dict[str, object]]:
        """Recent status changes and self-repairs, newest first."""
        events = list(self._history)[-limit:]
        return [
            {
                "at": event.at.isoformat(),
                "key": event.key,
                "label": event.label,
                "status": event.status,
                "detail": event.detail,
            }
            for event in reversed(events)
        ]

    @property
    def last_run_at(self) -> datetime | None:
        return self._last_run_at

    async def _loop(self) -> None:
        interval = max(30, settings.health_check_interval_seconds)
        try:
            while True:
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("Health monitor tick failed")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> bool:
        if not is_enabled():
            logger.info(
                "Health monitor disabled (needs HEALTH_MONITOR_ENABLED, SMTP config, "
                "and ALERT_EMAIL or ADMIN_EMAIL)"
            )
            return False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="health-monitor")
        logger.info(
            "Health monitor active: probing every %ds, alerting after %d consecutive failures",
            settings.health_check_interval_seconds,
            settings.health_failure_threshold,
        )
        return True

    def stop(self) -> None:
        for task in (self._task, self._run_task):
            if task is not None and not task.done():
                task.cancel()
        self._task = None
        self._run_task = None

    def reset_for_tests(self) -> None:
        global _repair_notice_sent_at, _repair_attempts
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
        self._states.clear()
        self._history.clear()
        self._last_run_at = None
        self._task = None
        self._run_task = None
        self._run = None
        # ``async with`` releases the lock even when the run is cancelled, so
        # this is not about a stuck lock. Tests share this singleton across
        # per-test event loops, and a cancelled run can leave a waiter future
        # belonging to a loop that is already closed; a fresh lock cannot.
        self._run_lock = asyncio.Lock()
        _repair_notice_sent_at = None
        _repair_attempts = 0


health_monitor = HealthMonitor()
