"""Operator alerting for failed agent tool calls (multi_user only).

Feeds the same ``AlertAggregator`` that carries application errors, so one
flush produces one email covering both. What this adds over the ERROR-log path
is the two failure kinds that never log at ERROR, ``SERVICE`` and ``AUTH``, and
per-tool attribution: the log-based alert fingerprints on the log *template*,
so every tool that raises collapses into a single group.

Design:

- **Only real faults.** ``ToolErrorKind`` distinguishes an integration being
  down from the model passing bad arguments. ``VALIDATION`` and ``NOT_FOUND``
  are the model self-correcting, and the error hint exists to make it retry;
  ``PERMISSION`` and ``INTERRUPTED`` are the user declining or stopping. None
  of those are incidents, and alerting on them trains the operator to ignore
  the channel.
- **Consent decides detail, not visibility.** Every qualifying failure raises
  the group's occurrence and distinct-user counts. Arguments and result text
  are attached only for users who opted into data sharing. An outage confined
  to non-consenting users is therefore still visible as a number, which a
  strict consent filter would have hidden entirely.
- **Consent is resolved inline, from cache.** Deferring it to flush time would
  mean holding arguments in memory for users who never consented, for the
  whole flush interval. A short TTL cache keeps the check to a dict lookup so
  non-consenting detail is dropped at the door.
- **Fails closed.** A cache miss counts the failure and withholds detail rather
  than blocking the agent loop on a database read. The cache warms in the
  background and the next occurrence carries detail. Being one occurrence late
  with a sample is cheaper than adding database latency to a live reply, and
  far cheaper than showing data whose consent state was assumed.
- **Never raises.** The hook swallows handler exceptions, but this also avoids
  logging at ERROR: an exception here would otherwise become an application
  alert about the alerting path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select

from backend.app.agent.tool_failure_hook import (
    ToolFailurePayload,
    set_tool_failure_handler,
)
from backend.app.agent.tools.base import ToolErrorKind
from backend.app.database import AsyncSessionLocal
from backend.app.models import User
from backend.app.services import admin_alerts
from backend.app.services.pii_redaction import redact_pii

logger = logging.getLogger(__name__)

# The kinds that mean something is broken rather than the agent working.
ALERTING_KINDS: frozenset[str] = frozenset(
    {
        str(ToolErrorKind.INTERNAL),
        str(ToolErrorKind.SERVICE),
        str(ToolErrorKind.AUTH),
    }
)

# Consent cache lifetime. Short because it bounds how long a revocation keeps
# leaking detail: a user who opts out is still treated as consenting until
# their entry expires.
_CONSENT_TTL_SECONDS = 60
_MAX_CONSENT_ENTRIES = 2000

_background_tasks: set[asyncio.Task[None]] = set()
_consent_cache: dict[str, tuple[bool, float]] = {}
_consent_lock = asyncio.Lock()
_warming: set[str] = set()


def _cached_consent(user_id: str) -> bool | None:
    """Return cached consent, or None when unknown or stale."""
    entry = _consent_cache.get(user_id)
    if entry is None:
        return None
    consented, expires_at = entry
    if time.monotonic() >= expires_at:
        _consent_cache.pop(user_id, None)
        return None
    return consented


def _store_consent(user_id: str, consented: bool) -> None:
    if len(_consent_cache) >= _MAX_CONSENT_ENTRIES:
        # Cheap bound. Dropping the whole cache costs one database read per
        # active user afterwards, which beats tracking an LRU for a value that
        # expires in a minute anyway.
        _consent_cache.clear()
    _consent_cache[user_id] = (consented, time.monotonic() + _CONSENT_TTL_SECONDS)


async def _warm_consent(user_id: str) -> None:
    """Populate the consent cache for *user_id* off the request path."""
    try:
        db = AsyncSessionLocal()
        try:
            consented = (
                await db.execute(select(User.data_sharing_consent).where(User.id == user_id))
            ).scalar_one_or_none()
        finally:
            await db.close()
        _store_consent(user_id, bool(consented))
    except Exception:
        logger.debug("Consent warm failed for %s", user_id, exc_info=True)
    finally:
        async with _consent_lock:
            _warming.discard(user_id)


# Per-side clip before admin_alerts applies its own overall cap.
_MAX_SIDE_CHARS = 240


def _format_sample(payload: ToolFailurePayload) -> str:
    """Render one failure's detail, redacted.

    Arguments are the highest-PII surface the agent touches (customer names,
    addresses, invoice contents), so even a consenting user's detail goes
    through the same redaction the admin console uses.
    """
    args = redact_pii(_compact(payload.args))[:_MAX_SIDE_CHARS]
    result = redact_pii(payload.result_text)[:_MAX_SIDE_CHARS]
    return f"args={args} -> {result}"


def _compact(args: dict[str, Any]) -> str:
    """One-line repr of tool arguments, with long values clipped."""
    parts: list[str] = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 120:
            text = text[:120] + "..."
        parts.append(f"{key}={text}")
    return "{" + ", ".join(parts) + "}"


async def handle_tool_failure(payload: ToolFailurePayload) -> None:
    """Record one tool failure for the operator alert email."""
    if payload.error_kind not in ALERTING_KINDS:
        return
    if not admin_alerts.is_enabled():
        return

    consented = _cached_consent(payload.user_id)
    if consented is None:
        # Fail closed and warm in the background, at most one task per user.
        async with _consent_lock:
            if payload.user_id not in _warming:
                _warming.add(payload.user_id)
                task = asyncio.create_task(_warm_consent(payload.user_id))
                # Hold a reference so the task is not garbage collected mid-flight.
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        consented = False

    sample = _format_sample(payload) if consented else None
    admin_alerts.record_tool_failure(
        tool_name=payload.tool_name,
        error_kind=payload.error_kind,
        user_id=payload.user_id,
        sample=sample,
    )


def install_tool_failure_alerts() -> None:
    """Route agent tool failures into the operator alert email."""
    set_tool_failure_handler(handle_tool_failure)
    logger.info("Tool failure alerting installed")


def reset_for_tests() -> None:
    """Clear cache and in-flight state. For tests."""
    _consent_cache.clear()
    _warming.clear()
    _background_tasks.clear()
