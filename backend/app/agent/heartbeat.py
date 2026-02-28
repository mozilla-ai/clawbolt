"""Proactive heartbeat engine.

Every ``heartbeat_interval_minutes`` the scheduler wakes up, iterates over
onboarded contractors, and runs **cheap deterministic checks** first.  Only when
a cheap check flags something actionable does the engine escalate to an LLM call
to compose a natural-language message.  Most ticks produce **no** outbound
messages and **no** LLM calls — saving cost and avoiding noise.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from dataclasses import dataclass, field

from any_llm import acompletion
from sqlalchemy.orm import Session

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import (
    Contractor,
    Estimate,
    HeartbeatChecklistItem,
    Memory,
    Message,
)
from backend.app.services.messaging import MessagingService, _build_messaging_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

HEARTBEAT_SYSTEM_PROMPT = """\
You are Backshop's heartbeat evaluator. Your job is to compose a short, \
actionable message for the contractor based on the flags below.

## About the contractor
{soul_prompt}

## Contractor's memory
{memory_context}

## Recent conversation (last 5 messages)
{recent_messages}

## Flags raised by pre-checks
{flags}

## Current time
{current_time}

## Rules
- The pre-checks already decided something needs attention. Your job is to \
compose one concise, helpful message.
- Combine multiple flags into a single message when possible.
- Keep the message under 160 characters.
- Be direct and actionable — no fluff.
- If after reviewing the flags you believe none actually warrant a message \
right now, you may still return "no_action".

Respond with ONLY a JSON object (no markdown fences):
{{"action": "send_message" | "no_action", "message": "...", "reasoning": "...", "priority": 1-5}}
"""

# Keywords that suggest a memory fact is time-sensitive
_TIME_KEYWORDS = re.compile(
    r"\b(remind|follow.?up|tomorrow|callback|check.?in|deadline|due|urgent)\b",
    re.IGNORECASE,
)

STALE_ESTIMATE_HOURS = 24


@dataclass
class CheapCheckResult:
    """Result of deterministic pre-checks for a single contractor."""

    flags: list[str] = field(default_factory=list)
    stale_estimates: list[Estimate] = field(default_factory=list)
    due_checklist_items: list[HeartbeatChecklistItem] = field(default_factory=list)
    time_sensitive_memories: list[Memory] = field(default_factory=list)

    @property
    def has_flags(self) -> bool:
        return len(self.flags) > 0


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
# Cheap checks — deterministic, no LLM call
# ---------------------------------------------------------------------------


def run_cheap_checks(
    db: Session,
    contractor: Contractor,
    now: datetime.datetime | None = None,
) -> CheapCheckResult:
    """Run fast, deterministic checks that don't require an LLM call.

    Returns a ``CheapCheckResult`` with flags describing what needs attention.
    If ``flags`` is empty, everything is clean and the LLM can be skipped.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    result = CheapCheckResult()

    # 1. Stale draft estimates (older than STALE_ESTIMATE_HOURS)
    cutoff = now - datetime.timedelta(hours=STALE_ESTIMATE_HOURS)
    stale = (
        db.query(Estimate)
        .filter(
            Estimate.contractor_id == contractor.id,
            Estimate.status == "draft",
            Estimate.created_at <= cutoff,
        )
        .all()
    )
    if stale:
        result.stale_estimates = list(stale)
        descs = ", ".join(e.description[:40] for e in stale)
        result.flags.append(f"Stale draft estimate(s) older than 24h: {descs}")

    # 2. Due checklist items
    active_items = (
        db.query(HeartbeatChecklistItem)
        .filter(
            HeartbeatChecklistItem.contractor_id == contractor.id,
            HeartbeatChecklistItem.status == "active",
        )
        .all()
    )
    for item in active_items:
        if _is_checklist_item_due(item, now):
            result.due_checklist_items.append(item)
            result.flags.append(f"Checklist item due: {item.description}")

    # 3. Time-sensitive memory facts
    memories = db.query(Memory).filter(Memory.contractor_id == contractor.id).all()
    for mem in memories:
        text = f"{mem.key} {mem.value}"
        if _TIME_KEYWORDS.search(text):
            result.time_sensitive_memories.append(mem)
            result.flags.append(f"Time-sensitive memory: {mem.key} = {mem.value}")

    return result


def _is_checklist_item_due(
    item: HeartbeatChecklistItem,
    now: datetime.datetime,
) -> bool:
    """Determine whether a checklist item should fire on this tick."""
    # Weekday gate applies regardless of trigger history
    if item.schedule == "weekdays" and now.weekday() > 4:
        return False

    # Never triggered → due (for daily/weekdays/once)
    if item.last_triggered_at is None:
        return True

    elapsed = now - item.last_triggered_at

    if item.schedule == "once":
        # Already triggered once → not due again
        return False
    # Default: "daily" or "weekdays" (weekday gate already passed above)
    return elapsed >= datetime.timedelta(hours=20)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


async def build_heartbeat_context(
    db: Session,
    contractor: Contractor,
    flags: list[str],
) -> dict[str, str]:
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

    return {
        "soul_prompt": soul_prompt,
        "memory_context": memory_context or "(none)",
        "recent_messages": "\n".join(recent_lines) or "(no recent messages)",
        "flags": "\n".join(f"- {f}" for f in flags),
        "current_time": datetime.datetime.now(datetime.UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# LLM evaluation (only called when cheap checks flag something)
# ---------------------------------------------------------------------------


async def evaluate_heartbeat_need(
    db: Session,
    contractor: Contractor,
    flags: list[str],
) -> HeartbeatAction:
    """Ask the LLM to compose a message based on flagged items."""
    ctx = await build_heartbeat_context(db, contractor, flags)
    prompt = HEARTBEAT_SYSTEM_PROMPT.format(**ctx)

    model = settings.heartbeat_model or settings.llm_model
    provider = settings.heartbeat_provider or settings.llm_provider

    response = await acompletion(
        model=model,
        provider=provider,
        api_base=settings.llm_api_base,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Compose a proactive message based on the flags above."},
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

    # Cheap checks — skip LLM entirely if nothing is flagged
    check_result = run_cheap_checks(db, contractor)
    if not check_result.has_flags:
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="All cheap checks clean — skipped LLM",
            priority=0,
        )

    # Something was flagged — escalate to LLM for message composition
    action = await evaluate_heartbeat_need(db, contractor, check_result.flags)

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

    # Mark checklist items as triggered
    now = datetime.datetime.now(datetime.UTC)
    for item in check_result.due_checklist_items:
        item.last_triggered_at = now
        if item.schedule == "once":
            item.status = "completed"
    if check_result.due_checklist_items:
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
