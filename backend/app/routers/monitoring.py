"""Health check endpoints plus the admin-only monitoring status view."""

import datetime
import logging
import time

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.admin_dep import get_current_admin
from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.models import User
from backend.app.services import admin_alerts, email_service, health_monitor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_start_time = time.monotonic()


# ``/premium-health`` is the original path, kept as an alias so external
# uptime monitors pointed at it keep working. Prefer ``/health/detail``.
@router.get("/health/detail")
@router.get("/premium-health", include_in_schema=False)
async def health_detail(db: AsyncSession = Depends(get_async_db)) -> dict:
    """Rich health check: status, database connectivity, uptime.

    ``/api/health`` answers the same status question and is what the
    platform healthcheck uses. This adds process uptime, which is what
    distinguishes "still degraded" from "just restarted".
    """
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.warning("Health check: database unreachable")

    uptime_seconds = int(time.monotonic() - _start_time)
    status = "healthy" if db_ok else "degraded"

    return {
        "status": status,
        "database": "connected" if db_ok else "unreachable",
        "uptime_seconds": uptime_seconds,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


@router.get("/monitoring/status")
async def monitoring_status(_admin: User = Depends(get_current_admin)) -> dict:
    """Current state of every health probe, plus alerting configuration.

    Answers "is it working right now" without waiting for the next email, and
    makes a misconfigured alert pipeline visible: ``alerts.enabled`` false with
    SMTP set means no recipient resolved.
    """
    return {
        "alerts": {
            "enabled": admin_alerts.is_enabled(),
            "pending_groups": admin_alerts.pending_group_count(),
            "dedupe_minutes": settings.alert_dedupe_minutes,
            "flush_interval_seconds": settings.alert_flush_interval_seconds,
            "max_emails_per_hour": settings.alert_max_emails_per_hour,
        },
        "health_monitor": {
            "enabled": health_monitor.is_enabled(),
            "interval_seconds": settings.health_check_interval_seconds,
            "failure_threshold": settings.health_failure_threshold,
            "probes": health_monitor.health_monitor.snapshot(),
            "last_run_at": (
                last_run.isoformat()
                if (last_run := health_monitor.health_monitor.last_run_at)
                else None
            ),
            # Status changes and self-repairs since this process started. In
            # memory, so a redeploy resets it; the alert emails are the durable
            # record.
            "history": health_monitor.health_monitor.history(),
            # Step-by-step state of the current or most recent run. Null before
            # the first one. Polled by the admin tab while ``running`` is true.
            "run": health_monitor.health_monitor.run_progress(),
        },
        "email": _email_status(),
        "recipient_configured": bool(settings.alert_email or settings.admin_email),
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def _email_status() -> dict:
    """Transport configuration plus the last send's outcome.

    A dormant or broken email path is the failure that hides every other
    failure, so the tab shows it next to the probes rather than leaving the
    operator to infer it from missing mail.
    """
    status = email_service.transport_status()
    return {
        "configured": status.configured,
        "host": status.host,
        "port": status.port,
        "timeout_seconds": status.timeout_seconds,
        "last_attempt_at": status.last_attempt_at.isoformat() if status.last_attempt_at else None,
        "last_success_at": status.last_success_at.isoformat() if status.last_success_at else None,
        "last_error": status.last_error,
        "last_error_at": status.last_error_at.isoformat() if status.last_error_at else None,
    }


@router.post("/monitoring/test-alert")
async def send_test_alert(_admin: User = Depends(get_current_admin)) -> dict:
    """Send a synthetic alert email so SMTP and recipient wiring can be verified.

    Worth having because the alert path is, by design, exercised only when
    something is already broken. Discovering then that ``ALERT_EMAIL`` had a
    typo is the worst possible time.

    On failure the transport's own explanation is returned rather than a generic
    "not sent", which previously sent the operator to the container logs to
    learn whether the cause was a typo, a rejected password, or a blocked port.
    """
    sent = await admin_alerts.send_test_alert()
    status = email_service.transport_status()
    if sent:
        detail = "Test alert sent."
    elif not status.configured:
        detail = "Not sent: SMTP is not configured (SMTP_HOST and SMTP_FROM_EMAIL are unset)."
    elif not admin_alerts.is_enabled():
        detail = (
            "Not sent: alerting is off. Needs ALERTS_ENABLED, SMTP config, and "
            "ALERT_EMAIL or ADMIN_EMAIL."
        )
    else:
        detail = status.last_error or "Not sent: the send failed without reporting a reason."
    return {
        "sent": sent,
        "recipient_configured": bool(settings.alert_email or settings.admin_email),
        "detail": detail,
        "email": _email_status(),
    }


@router.post("/monitoring/diagnose-email")
async def diagnose_email(_admin: User = Depends(get_current_admin)) -> dict:
    """Probe the email path from inside this container and explain the result.

    Answers the question a failed test alert leaves open: is the mail server
    saying no, or is nothing getting out of the container at all? Reports
    per-port TCP reachability plus a full authenticated handshake (EHLO,
    STARTTLS, login, NOOP) against the configured port, sending no message.
    """
    return await email_service.diagnose_transport()


@router.post("/monitoring/run-probes")
async def run_probes(_admin: User = Depends(get_current_admin)) -> dict:
    """Start a probe run in the background and return its initial progress.

    Deliberately does not await the run. A full pass calls an LLM provider, a
    residential Mac, and one auth_check per specialist per
    user; awaiting it held the request open for minutes with nothing to show,
    and any proxy timeout in between lost the result entirely. The caller polls
    ``GET /monitoring/status`` and reads ``health_monitor.run`` instead.
    """
    started = health_monitor.health_monitor.start_run("manual")
    return {
        "started": started,
        "detail": (
            "Probe run started."
            if started
            else "A probe run is already in flight; watching that one instead."
        ),
        "run": health_monitor.health_monitor.run_progress(),
    }
