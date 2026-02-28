"""Proactive heartbeat engine.

Every ``heartbeat_interval_minutes`` the scheduler wakes up, iterates over
onboarded contractors, and asks the LLM whether any proactive outreach is
warranted.  Most ticks produce **no** outbound messages — only genuinely
useful reminders or follow-ups trigger SMS.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from dataclasses import dataclass

from any_llm import acompletion
from sqlalchemy.orm import Session

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import Contractor, Estimate, Message
from backend.app.services.messaging import MessagingService, _build_messaging_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

HEARTBEAT_SYSTEM_PROMPT = """\
You are Backshop's heartbeat evaluator. Your job is to decide whether the \
contractor needs a proactive check-in message RIGHT NOW.

## About the contractor
{soul_prompt}

## Contractor's memory
{memory_context}

## Recent conversation (last 5 messages)
{recent_messages}

## Pending estimates
{pending_estimates}

## Current time
{current_time}

## Rules
- Most of the time you should answer NO — do not nag.
- Only reach out for genuinely actionable items:
  * Estimate drafts older than 24 h that haven't been sent
  * Follow-up questions the contractor asked you to remind them about
  * Useful daily summary if there is something worth summarising
- NEVER reach out just to say hi or ask how the day is going.
- Keep the message under 160 characters (single SMS segment).

Respond with ONLY a JSON object (no markdown fences):
{{"action": "send_message" | "no_action", "message": "...", "reasoning": "...", "priority": 1-5}}
"""


@dataclass
class HeartbeatAction:
    action_type: str  # "send_message" or "no_action"
    message: str
    reasoning: str
    priority: int


# ---------------------------------------------------------------------------
# Business-hours helpers
# ---------------------------------------------------------------------------

_AMPM_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*[-\u2013to]+\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
    re.IGNORECASE,
)
_24H_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-\u2013to]+\s*(\d{1,2}):(\d{2})")


def _parse_business_hours(hours_str: str) -> tuple[int, int] | None:
    """Parse common business-hours strings into (start_hour, end_hour).

    Supports formats like ``"7am-5pm"``, ``"7:00am - 5:00pm"``, ``"08:00-17:00"``.
    Returns *None* when the string cannot be parsed.
    """
    m = _AMPM_RE.search(hours_str)
    if m:
        start_h = int(m.group(1))
        start_ampm = m.group(3).lower()
        end_h = int(m.group(4))
        end_ampm = m.group(6).lower()

        if start_ampm == "pm" and start_h != 12:
            start_h += 12
        elif start_ampm == "am" and start_h == 12:
            start_h = 0

        if end_ampm == "pm" and end_h != 12:
            end_h += 12
        elif end_ampm == "am" and end_h == 12:
            end_h = 0

        return start_h, end_h

    m = _24H_RE.search(hours_str)
    if m:
        return int(m.group(1)), int(m.group(3))

    return None


def is_within_business_hours(
    contractor: Contractor,
    now: datetime.datetime | None = None,
) -> bool:
    """Return *True* if *now* falls within the contractor's business hours.

    Falls back to the global quiet-hours window from settings when the
    contractor has not set ``business_hours``.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    current_hour = now.hour

    if contractor.business_hours:
        parsed = _parse_business_hours(contractor.business_hours)
        if parsed:
            start, end = parsed
            if start <= end:
                return start <= current_hour < end
            # Overnight range (e.g. 22-6) — unlikely but handle it
            return current_hour >= start or current_hour < end

    # Fallback: outside quiet hours means "business hours"
    qstart = settings.heartbeat_quiet_hours_start
    qend = settings.heartbeat_quiet_hours_end
    if qstart > qend:
        # Quiet hours span midnight (e.g. 20-7)
        in_quiet = current_hour >= qstart or current_hour < qend
    else:
        in_quiet = qstart <= current_hour < qend
    return not in_quiet


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


async def build_heartbeat_context(db: Session, contractor: Contractor) -> dict[str, str]:
    """Gather all context needed for the heartbeat LLM evaluation."""
    soul_prompt = build_soul_prompt(contractor)
    memory_context = await build_memory_context(db, contractor.id)

    # Recent messages (last 5)
    conv, _ = await get_or_create_conversation(db, contractor.id)
    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .limit(5)
        .all()
    )
    recent_lines: list[str] = []
    for msg in reversed(recent):
        direction = "Contractor" if msg.direction == "inbound" else "Backshop"
        recent_lines.append(f"[{direction}] {msg.body}")

    # Pending estimates
    pending = (
        db.query(Estimate)
        .filter(Estimate.contractor_id == contractor.id, Estimate.status == "draft")
        .all()
    )
    estimate_lines = [
        f"- #{e.id}: {e.description[:80]} (${e.total_amount:.0f}, created {e.created_at})"
        for e in pending
    ]

    return {
        "soul_prompt": soul_prompt,
        "memory_context": memory_context or "(none)",
        "recent_messages": "\n".join(recent_lines) or "(no recent messages)",
        "pending_estimates": "\n".join(estimate_lines) or "(none)",
        "current_time": datetime.datetime.now(datetime.UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# LLM evaluation
# ---------------------------------------------------------------------------


async def evaluate_heartbeat_need(db: Session, contractor: Contractor) -> HeartbeatAction:
    """Ask the LLM whether a proactive message is warranted."""
    ctx = await build_heartbeat_context(db, contractor)
    prompt = HEARTBEAT_SYSTEM_PROMPT.format(**ctx)

    response = await acompletion(
        model=settings.llm_model,
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Evaluate whether to send a proactive message now."},
        ],
        max_tokens=300,
    )
    raw = response.choices[0].message.content or ""

    try:
        data = json.loads(raw)
        return HeartbeatAction(
            action_type=data.get("action", "no_action"),
            message=data.get("message", ""),
            reasoning=data.get("reasoning", ""),
            priority=int(data.get("priority", 3)),
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        logger.warning("Heartbeat LLM returned unparseable response: %s", raw[:200])
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning=f"Unparseable LLM response: {raw[:100]}",
            priority=0,
        )


# ---------------------------------------------------------------------------
# Per-contractor runner
# ---------------------------------------------------------------------------


async def run_heartbeat_for_contractor(
    db: Session,
    contractor: Contractor,
    messaging_service: MessagingService,
    daily_counts: dict[int, int],
    max_daily: int,
) -> HeartbeatAction | None:
    """Full heartbeat pipeline for a single contractor.

    Returns the action taken, or *None* if skipped.
    """
    # Gate: onboarding must be complete
    if not contractor.onboarding_complete:
        return None

    # Gate: business hours
    if not is_within_business_hours(contractor):
        return None

    # Gate: daily rate limit
    if daily_counts.get(contractor.id, 0) >= max_daily:
        return None

    action = await evaluate_heartbeat_need(db, contractor)

    if action.action_type != "send_message" or not action.message:
        return action

    # Send message
    to_address = contractor.channel_identifier or contractor.phone
    try:
        await messaging_service.send_text(to=to_address, body=action.message)
    except Exception:
        logger.exception("Heartbeat message failed for contractor %d", contractor.id)
        return action

    # Record outbound message
    conv, _ = await get_or_create_conversation(db, contractor.id)
    outbound = Message(
        conversation_id=conv.id,
        direction="outbound",
        body=action.message,
    )
    db.add(outbound)
    db.commit()

    daily_counts[contractor.id] = daily_counts.get(contractor.id, 0) + 1
    return action


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class HeartbeatScheduler:
    """Manages the periodic heartbeat loop as an asyncio background task."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._daily_counts: dict[int, int] = {}
        self._last_reset_date: datetime.date | None = None

    # -- public API --

    def start(self) -> None:
        """Start the heartbeat loop (idempotent)."""
        if not settings.heartbeat_enabled:
            logger.info("Heartbeat disabled via config")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_event_loop().create_task(self._run())
        logger.info(
            "Heartbeat started (interval=%dm, max_daily=%d)",
            settings.heartbeat_interval_minutes,
            settings.heartbeat_max_daily_messages,
        )

    def stop(self) -> None:
        """Cancel the background task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            logger.info("Heartbeat stopped")

    # -- internals --

    async def _run(self) -> None:
        """Loop forever, running one tick per interval."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heartbeat tick failed")
            await asyncio.sleep(settings.heartbeat_interval_minutes * 60)

    async def tick(self) -> None:
        """Single heartbeat pass: evaluate every onboarded contractor."""
        today = datetime.date.today()
        if self._last_reset_date != today:
            self._daily_counts = {}
            self._last_reset_date = today

        db: Session = SessionLocal()
        try:
            contractors = (
                db.query(Contractor).filter(Contractor.onboarding_complete.is_(True)).all()
            )
            messaging_service = _build_messaging_service()

            for contractor in contractors:
                try:
                    await run_heartbeat_for_contractor(
                        db=db,
                        contractor=contractor,
                        messaging_service=messaging_service,
                        daily_counts=self._daily_counts,
                        max_daily=settings.heartbeat_max_daily_messages,
                    )
                except Exception:
                    logger.exception("Heartbeat failed for contractor %d", contractor.id)
        finally:
            db.close()


# Module-level singleton used by main.py lifespan
heartbeat_scheduler = HeartbeatScheduler()
