"""Admin error alerting: turn application-level ERROR logs into operator email.

Reactive half of the monitoring stack. ``health_monitor`` is the proactive half;
this module answers "something just broke for a user, tell me now", while the
health monitor answers "something is broken and nobody has hit it yet".

Design:

- **A logging handler, not call sites.** Every error either layer already logs
  becomes an alert, including the ones nobody thought to instrument. Adding a
  new failure path costs nothing: ``logger.exception(...)`` is already the
  convention in both repos.
- **Grouped by fingerprint.** A broken integration logs the same error
  thousands of times a minute. Records collapse on
  ``(logger, exception type, log template)`` and each group emails at most
  once per ``alert_dedupe_minutes``, carrying the suppressed occurrence count.
  The log *template* (``record.msg`` before %-substitution) is the grouping
  key rather than the formatted message, so "user abc failed" and "user def
  failed" are one group instead of two.
- **Batched.** A flush loop coalesces every eligible group into a single email
  per tick, so one deploy going bad is one email listing five problems.
- **Never blocks the logging call.** ``emit()`` only mutates an in-memory dict
  under a lock. SMTP happens on the flush task. Logging is called from sync
  code, worker threads, and inside exception handlers; doing I/O there would
  be a deadlock and latency risk.
- **Tracebacks are formatted immediately.** Holding a live ``exc_info`` tuple
  pins every frame's locals (request objects, DB sessions, LLM payloads) alive
  for the whole dedupe window. Format to text on capture, drop the tuple.
- **Dormant without config.** No SMTP host or no resolvable recipient means
  the handler is never installed, so dev/local and CI never send.

Privacy: alert bodies carry tracebacks and log messages, which can contain user
data. They go only to the single operator address resolved from
``alert_email`` / ``admin_email``, and the alert body is never itself logged.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.app.config import settings
from backend.app.observability import get_request_id
from backend.app.services import email_service

logger = logging.getLogger(__name__)

# Logger trees whose ERROR records become alerts. ``uvicorn.error`` carries
# unhandled ASGI exceptions ("Exception in ASGI application"), which never
# reach a ``backend`` logger, so a 500 from a route would otherwise be
# invisible here.
CAPTURED_LOGGERS = ("backend", "uvicorn.error")

# Never capture from the alert delivery path itself. Without this, an SES
# outage logs ERROR from email_service, which enqueues an alert about the
# failure to send alerts, which fails to send, forever.
EXCLUDED_LOGGERS = (
    "backend.app.services.admin_alerts",
    "backend.app.services.email_service",
)

# Truncation bounds. A runaway recursion produces megabyte tracebacks, and an
# email with 200 distinct problems in it is unreadable anyway.
_MAX_TRACEBACK_CHARS = 4000
_MAX_MESSAGE_CHARS = 1000
_MAX_PENDING_GROUPS = 200
_MAX_GROUPS_PER_EMAIL = 25


def _recipient() -> str:
    """Resolve the operator address, preferring the alert-specific override."""
    return settings.alert_email or settings.admin_email


def is_enabled() -> bool:
    """True when alerts are configured well enough to actually deliver."""
    return bool(
        settings.alerts_enabled and settings.smtp_host and settings.smtp_from_email and _recipient()
    )


def _should_capture(logger_name: str) -> bool:
    """True when *logger_name* is in a captured tree and not on the send path.

    Excluded prefixes are checked first so a more specific exclusion beats the
    broad ``backend`` inclusion.
    """
    if logger_name.startswith(EXCLUDED_LOGGERS):
        return False
    return logger_name.startswith(CAPTURED_LOGGERS)


@dataclass(frozen=True)
class AlertSummary:
    """One grouped error, ready to render into the operator email."""

    title: str
    logger_name: str
    level: str
    count: int
    message: str
    traceback_text: str | None
    request_id: str
    first_seen: datetime
    last_seen: datetime


@dataclass
class _PendingGroup:
    """Mutable accumulator for one fingerprint between flushes."""

    logger_name: str
    level: str
    exc_type: str | None
    message: str
    traceback_text: str | None
    request_id: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1

    def merge(self, message: str, traceback_text: str | None, request_id: str) -> None:
        """Fold a repeat occurrence in, keeping the most recent detail.

        The newest traceback wins over the first: when an error recurs, the
        latest occurrence is the one still reproducible.
        """
        self.count += 1
        self.last_seen = datetime.now(UTC)
        self.message = message
        if traceback_text is not None:
            self.traceback_text = traceback_text
        if request_id != "-":
            self.request_id = request_id

    def to_summary(self) -> AlertSummary:
        exc = f"{self.exc_type}: " if self.exc_type else ""
        return AlertSummary(
            title=f"{exc}{self.message}"[:160],
            logger_name=self.logger_name,
            level=self.level,
            count=self.count,
            message=self.message,
            traceback_text=self.traceback_text,
            request_id=self.request_id,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
        )


@dataclass
class _AlertStore:
    """Thread-safe accumulator + throttle state.

    ``_pending`` is written from arbitrary threads via ``emit()`` and drained
    by the flush task, so every access takes ``_lock``. The lock is held only
    for dict mutation, never across the SMTP call.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: dict[str, _PendingGroup] = field(default_factory=dict)
    _last_emailed: dict[str, float] = field(default_factory=dict)
    _email_times: deque[float] = field(default_factory=deque)
    _overflow_dropped: int = 0

    def record(
        self,
        fingerprint: str,
        logger_name: str,
        level: str,
        exc_type: str | None,
        message: str,
        traceback_text: str | None,
        request_id: str,
    ) -> None:
        """Accumulate one error occurrence. Cheap, non-blocking, thread-safe."""
        now = datetime.now(UTC)
        with self._lock:
            existing = self._pending.get(fingerprint)
            if existing is not None:
                existing.merge(message, traceback_text, request_id)
                return
            if len(self._pending) >= _MAX_PENDING_GROUPS:
                # Distinct-fingerprint flood. Drop rather than grow unbounded;
                # the count surfaces in the next email so the truncation is
                # never silent.
                self._overflow_dropped += 1
                return
            self._pending[fingerprint] = _PendingGroup(
                logger_name=logger_name,
                level=level,
                exc_type=exc_type,
                message=message,
                traceback_text=traceback_text,
                request_id=request_id,
                first_seen=now,
                last_seen=now,
            )

    def _take_eligible(self) -> tuple[list[tuple[str, AlertSummary]], int]:
        """Remove and return groups past their dedupe cooldown.

        Returns ``(eligible, dropped)``. Groups still inside their cooldown
        stay pending and keep accumulating, so the eventual email reports the
        true occurrence count rather than resetting.
        """
        cooldown = max(0, settings.alert_dedupe_minutes) * 60
        cap = max(1, settings.alert_max_emails_per_hour)
        now = time.monotonic()
        with self._lock:
            # Hourly cap counts emails sent, not alerts raised.
            while self._email_times and now - self._email_times[0] > 3600:
                self._email_times.popleft()
            if len(self._email_times) >= cap:
                return [], 0

            eligible: list[tuple[str, AlertSummary]] = []
            for fingerprint, group in list(self._pending.items()):
                last = self._last_emailed.get(fingerprint)
                if last is not None and now - last < cooldown:
                    continue
                eligible.append((fingerprint, group.to_summary()))
                del self._pending[fingerprint]
                if len(eligible) >= _MAX_GROUPS_PER_EMAIL:
                    break

            dropped = self._overflow_dropped
            if eligible:
                self._overflow_dropped = 0

            # Prune cooldown bookkeeping for fingerprints nobody is hitting
            # any more, so a long-lived process does not leak keys.
            stale_cutoff = now - max(cooldown * 2, 3600)
            for fingerprint, stamp in list(self._last_emailed.items()):
                if stamp < stale_cutoff and fingerprint not in self._pending:
                    del self._last_emailed[fingerprint]

            return eligible, dropped

    def _mark_emailed(self, fingerprints: list[str]) -> None:
        """Start the cooldown for successfully emailed fingerprints."""
        now = time.monotonic()
        with self._lock:
            self._email_times.append(now)
            for fingerprint in fingerprints:
                self._last_emailed[fingerprint] = now

    async def flush(self) -> bool:
        """Send one batched email for everything currently eligible.

        Returns True when an email was sent. Best-effort: on send failure the
        cooldown is deliberately *not* started, so the next occurrence tries
        again rather than going quiet for the whole dedupe window. The
        accumulated count for the failed batch is lost; the underlying errors
        are still in the application log.
        """
        eligible, dropped = self._take_eligible()
        if not eligible:
            return False
        summaries = [summary for _, summary in eligible]
        sent = await email_service.send_admin_alert(_recipient(), summaries, dropped)
        if sent:
            self._mark_emailed([fingerprint for fingerprint, _ in eligible])
        return sent

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def reset(self) -> None:
        """Clear all state. For tests."""
        with self._lock:
            self._pending.clear()
            self._last_emailed.clear()
            self._email_times.clear()
            self._overflow_dropped = 0


_store = _AlertStore()


class AdminAlertHandler(logging.Handler):
    """Logging handler that accumulates ERROR records for the operator email."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        """Fingerprint and accumulate. Must never raise, never block."""
        try:
            if not _should_capture(record.name):
                return

            exc_type: str | None = None
            traceback_text: str | None = None
            if record.exc_info and record.exc_info[1] is not None:
                exc_class, exc_value, exc_tb = record.exc_info
                exc_type = type(exc_value).__name__ if exc_class is not None else None
                # Format now: holding exc_tb would pin every frame's locals
                # alive for the whole dedupe window.
                traceback_text = "".join(traceback.format_exception(exc_class, exc_value, exc_tb))[
                    :_MAX_TRACEBACK_CHARS
                ]

            # Group on the unformatted template so per-user / per-id variation
            # in the arguments collapses into one alert.
            template = str(record.msg)
            fingerprint = f"{record.name}|{exc_type or ''}|{template}"

            _store.record(
                fingerprint=fingerprint,
                logger_name=record.name,
                level=record.levelname,
                exc_type=exc_type,
                message=record.getMessage()[:_MAX_MESSAGE_CHARS],
                traceback_text=traceback_text,
                request_id=get_request_id(),
            )
        except Exception:
            # Handler contract: never propagate out of a logging call.
            self.handleError(record)


_handler: AdminAlertHandler | None = None
_flush_task: asyncio.Task[None] | None = None


def install_alert_handler() -> bool:
    """Attach the handler to the captured logger trees. Idempotent.

    Returns True when installed (or already installed), False when alerting is
    not configured.

    Attached per-tree rather than on the root logger because ``uvicorn.error``
    propagates to ``uvicorn``, which uvicorn's own log config marks
    ``propagate: False`` -- records from it never reach root handlers.

    Safe to call more than once, and it must be: uvicorn applies its
    ``dictConfig`` after this module's import-time install, and ``dictConfig``
    strips existing handlers from every logger it names (including
    ``uvicorn.error``). ``start_alert_flusher()`` re-installs from the
    lifespan, which runs after that.
    """
    global _handler
    if not is_enabled():
        return False
    if _handler is None:
        _handler = AdminAlertHandler()
    for name in CAPTURED_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(h, AdminAlertHandler) for h in target.handlers):
            target.addHandler(_handler)
    return True


async def _flush_loop() -> None:
    """Drain accumulated alerts on a timer until cancelled."""
    interval = max(5, settings.alert_flush_interval_seconds)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await _store.flush()
            except Exception:
                # Excluded logger, so this cannot feed back into the store.
                logger.exception("Admin alert flush failed")
    except asyncio.CancelledError:
        # Final best-effort drain so a clean shutdown does not swallow the
        # errors that may have caused it.
        try:
            await _store.flush()
        except Exception:
            logger.exception("Admin alert final flush failed")
        raise


def start_alert_flusher() -> bool:
    """Re-install the handler and start the flush task. Call from the lifespan."""
    global _flush_task
    if not install_alert_handler():
        logger.info("Admin error alerts disabled (no SMTP host or no recipient configured)")
        return False
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(_flush_loop(), name="admin-alert-flush")
    logger.info(
        "Admin error alerts active: grouped ERROR logs email every %ds, "
        "at most one per fingerprint per %dm",
        settings.alert_flush_interval_seconds,
        settings.alert_dedupe_minutes,
    )
    return True


def stop_alert_flusher() -> None:
    """Cancel the flush task. Call from lifespan shutdown."""
    global _flush_task
    if _flush_task is not None and not _flush_task.done():
        _flush_task.cancel()
    _flush_task = None


def pending_group_count() -> int:
    """Number of error groups waiting for the next flush."""
    return _store.pending_count()


async def send_test_alert() -> bool:
    """Send a synthetic alert so an operator can verify the pipeline end to end.

    Bypasses the store entirely: dedupe and the hourly cap would otherwise make
    a repeated test look like a broken pipeline.
    """
    if not is_enabled():
        return False
    now = datetime.now(UTC)
    summary = AlertSummary(
        title="Test alert: alerting is configured correctly",
        logger_name="backend.app.services.admin_alerts",
        level="ERROR",
        count=1,
        message=(
            "This is a synthetic alert triggered from the admin monitoring endpoint. "
            "No application error occurred."
        ),
        traceback_text=None,
        request_id=get_request_id(),
        first_seen=now,
        last_seen=now,
    )
    return await email_service.send_admin_alert(_recipient(), [summary], 0)


def reset_for_tests() -> None:
    """Detach the handler and clear state so tests do not leak into each other."""
    global _handler, _flush_task
    for name in CAPTURED_LOGGERS:
        target = logging.getLogger(name)
        for handler in [h for h in target.handlers if isinstance(h, AdminAlertHandler)]:
            target.removeHandler(handler)
    _handler = None
    _flush_task = None
    _store.reset()
