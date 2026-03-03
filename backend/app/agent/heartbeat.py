"""Proactive heartbeat engine.

Every ``heartbeat_interval_minutes`` the scheduler wakes up, iterates over
onboarded contractors, and runs **cheap deterministic checks** first.  Only when
a cheap check flags something actionable does the engine escalate to an LLM call
to compose a natural-language message.  Most ticks produce **no** outbound
messages and **no** LLM calls -- saving cost and avoiding noise.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, cast

from any_llm import acompletion
from any_llm.types.completion import ChatCompletion
from sqlalchemy.orm import Session

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.llm_parsing import parse_tool_calls
from backend.app.agent.system_prompt import build_heartbeat_system_prompt
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import (
    ChecklistSchedule,
    ChecklistStatus,
    EstimateStatus,
    MessageDirection,
)
from backend.app.models import (
    Contractor,
    Conversation,
    Estimate,
    HeartbeatChecklistItem,
    HeartbeatLog,
    Memory,
    Message,
)
from backend.app.services.llm_usage import log_llm_usage
from backend.app.services.messaging import MessagingService, _build_messaging_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Tool schema for the heartbeat compose_message tool
COMPOSE_MESSAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ToolName.COMPOSE_MESSAGE,
        "description": (
            "Compose a proactive message to send to the contractor, or decide no message is needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send_message", "no_action"],
                },
                "message": {
                    "type": "string",
                    "description": "The message to send (required if action is send_message)",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation of why this action was chosen",
                },
                "priority": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Priority level from 1 (lowest) to 5 (highest)",
                },
            },
            "required": ["action", "reasoning", "priority"],
        },
    },
}

# Keywords that suggest a memory fact is time-sensitive
_TIME_KEYWORDS = re.compile(
    r"\b(remind|follow.?up|tomorrow|callback|check.?in|deadline|due|urgent)\b",
    re.IGNORECASE,
)


STALE_ESTIMATE_HOURS = settings.heartbeat_stale_estimate_hours
IDLE_DAYS = settings.heartbeat_idle_days
CHECKLIST_DAILY_INTERVAL_HOURS = settings.checklist_daily_interval_hours
HEARTBEAT_RECENT_MESSAGES_COUNT = settings.heartbeat_recent_messages_count
WEEKDAY_FRIDAY = 4  # Monday=0 ... Friday=4

_FREQ_RE = re.compile(r"^(\d+)\s*(m|h|d)(?:in(?:utes?)?|ours?|ays?)?$", re.IGNORECASE)


def parse_frequency_to_minutes(freq: str) -> int | None:
    """Parse a human-friendly frequency string into minutes.

    Supports formats like ``"30m"``, ``"1h"``, ``"2d"``, ``"daily"``.
    Returns *None* when the string is empty or cannot be parsed.
    """
    freq = freq.strip()
    if not freq:
        return None
    if freq.lower() == "daily":
        return 1440  # 24 * 60
    m = _FREQ_RE.match(freq)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    return None


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


def _to_local_time(
    now: datetime.datetime,
    tz_name: str,
) -> datetime.datetime:
    """Convert *now* to the given IANA timezone, returning *now* unchanged on error."""
    if not tz_name:
        return now
    try:
        return now.astimezone(zoneinfo.ZoneInfo(tz_name))
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
        return now


def is_within_business_hours(
    contractor: Contractor,
    now: datetime.datetime | None = None,
) -> bool:
    """Return *True* if *now* falls within the contractor's business hours.

    When the contractor has a ``timezone`` set, *now* is converted to their
    local time before comparing against business hours or the global quiet
    hours window.  Falls back to UTC when the timezone is empty or invalid.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    local_now = _to_local_time(now, contractor.timezone)
    current_hour = local_now.hour

    if contractor.business_hours:
        parsed = _parse_business_hours(contractor.business_hours)
        if parsed:
            start, end = parsed
            if start <= end:
                return start <= current_hour < end
            # Overnight range (e.g. 22-6), unlikely but handle it
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
# Cheap checks -- deterministic, no LLM call
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
            Estimate.status == EstimateStatus.DRAFT,
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
            HeartbeatChecklistItem.status == ChecklistStatus.ACTIVE,
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

    # 4. Idle contractor -- no inbound messages for IDLE_DAYS
    idle_cutoff = now - datetime.timedelta(days=IDLE_DAYS)
    last_inbound = (
        db.query(Message.created_at)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Conversation.contractor_id == contractor.id,
            Message.direction == MessageDirection.INBOUND,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_inbound is not None:
        last_ts = last_inbound[0]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=datetime.UTC)
        if last_ts <= idle_cutoff:
            days = (now - last_ts).days
            result.flags.append(f"Contractor idle for {days} days -- no recent messages")
    elif contractor.created_at is not None:
        created = contractor.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.UTC)
        if created <= idle_cutoff:
            days = (now - created).days
            result.flags.append(f"Contractor idle for {days} days -- no messages since onboarding")

    return result


def _is_checklist_item_due(
    item: HeartbeatChecklistItem,
    now: datetime.datetime,
) -> bool:
    """Determine whether a checklist item should fire on this tick."""
    # Weekday gate applies regardless of trigger history
    if item.schedule == ChecklistSchedule.WEEKDAYS and now.weekday() > WEEKDAY_FRIDAY:
        return False

    # Never triggered -> due (for daily/weekdays/once)
    if item.last_triggered_at is None:
        return True

    last = item.last_triggered_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.UTC)
    elapsed = now - last

    if item.schedule == ChecklistSchedule.ONCE:
        # Already triggered once -> not due again
        return False
    # Default: "daily" or "weekdays" (weekday gate already passed above)
    return elapsed >= datetime.timedelta(hours=CHECKLIST_DAILY_INTERVAL_HOURS)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _load_recent_messages(db: Session, contractor: Contractor) -> str:
    """Load recent messages as formatted text for heartbeat context."""
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.contractor_id == contractor.id,
            Conversation.is_active.is_(True),
        )
        .order_by(Conversation.last_message_at.desc())
        .first()
    )
    if conv is None:
        return "(no recent messages)"

    recent = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .limit(HEARTBEAT_RECENT_MESSAGES_COUNT)
        .all()
    )
    lines: list[str] = []
    for msg in reversed(recent):
        direction = "Contractor" if msg.direction == MessageDirection.INBOUND else "Backshop"
        lines.append(f"[{direction}] {msg.body}")
    return "\n".join(lines) or "(no recent messages)"


async def build_heartbeat_context(
    db: Session,
    contractor: Contractor,
    flags: list[str],
) -> str:
    """Build the full heartbeat system prompt via the composable builder."""
    recent_messages = _load_recent_messages(db, contractor)
    return await build_heartbeat_system_prompt(db, contractor, flags, recent_messages)


# ---------------------------------------------------------------------------
# Tool call response parsing
# ---------------------------------------------------------------------------


def _parse_tool_call_response(response: ChatCompletion) -> HeartbeatAction:
    """Extract a HeartbeatAction from an LLM tool call response.

    If the LLM did not call the compose_message tool (e.g. returned plain text
    instead), falls back to no_action.
    """
    parsed = parse_tool_calls(response)

    if not parsed:
        # LLM returned text instead of calling the tool: default to no_action
        content = response.choices[0].message.content or ""
        logger.warning("Heartbeat LLM returned text instead of tool call: %s", content[:200])
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning=f"LLM did not call compose_message tool: {content[:100]}",
            priority=0,
        )

    # Use the first tool call
    tc = parsed[0]
    if tc.name != ToolName.COMPOSE_MESSAGE:
        logger.warning("Heartbeat LLM called unexpected tool: %s", tc.name)
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="LLM called unexpected tool",
            priority=0,
        )

    if tc.arguments is None:
        logger.warning("Heartbeat tool call had malformed arguments")
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="Malformed tool arguments",
            priority=0,
        )

    data = tc.arguments
    try:
        priority = int(data.get("priority", 3))
    except (ValueError, TypeError):
        priority = 3

    return HeartbeatAction(
        action_type=data.get("action", "no_action"),
        message=data.get("message", ""),
        reasoning=data.get("reasoning", ""),
        priority=priority,
    )


# ---------------------------------------------------------------------------
# LLM evaluation (only called when cheap checks flag something)
# ---------------------------------------------------------------------------


async def evaluate_heartbeat_need(
    db: Session,
    contractor: Contractor,
    flags: list[str],
) -> HeartbeatAction:
    """Ask the LLM to compose a message based on flagged items.

    Uses the compose_message tool calling protocol instead of raw JSON parsing.
    If the LLM does not call the tool, defaults to no_action.
    """
    prompt = await build_heartbeat_context(db, contractor, flags)

    model = settings.heartbeat_model or settings.llm_model
    provider = settings.heartbeat_provider or settings.llm_provider

    response = cast(
        ChatCompletion,
        await acompletion(
            model=model,
            provider=provider,
            api_base=settings.llm_api_base,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": "Compose a proactive message based on the flags above.",
                },
            ],
            tools=[COMPOSE_MESSAGE_TOOL],
            max_tokens=settings.llm_max_tokens_heartbeat,
        ),
    )

    log_llm_usage(db, contractor.id, model, response, "heartbeat")
    return _parse_tool_call_response(response)


# ---------------------------------------------------------------------------
# Persistent rate limiting
# ---------------------------------------------------------------------------


def get_daily_heartbeat_count(db: Session, contractor_id: int) -> int:
    """Count heartbeat messages sent to a contractor today (UTC).

    Queries the ``heartbeat_log`` table instead of relying on in-memory state
    so that rate limits survive process restarts and work across multiple
    workers.
    """
    today_start = datetime.datetime.combine(
        datetime.datetime.now(datetime.UTC).date(), datetime.time.min, tzinfo=datetime.UTC
    )
    count: int = (
        db.query(HeartbeatLog)
        .filter(
            HeartbeatLog.contractor_id == contractor_id,
            HeartbeatLog.created_at >= today_start,
        )
        .count()
    )
    return count


# ---------------------------------------------------------------------------
# Per-contractor runner
# ---------------------------------------------------------------------------


async def run_heartbeat_for_contractor(
    db: Session,
    contractor: Contractor,
    messaging_service: MessagingService,
    max_daily: int,
) -> HeartbeatAction | None:
    """Full heartbeat pipeline for a single contractor.

    Returns the action taken, or *None* if skipped.
    """
    # Gate: onboarding must be complete
    if not contractor.onboarding_complete:
        return None

    # Gate: contractor heartbeat opt-in
    if not contractor.heartbeat_opt_in:
        return None

    # Gate: business hours
    if not is_within_business_hours(contractor):
        return None

    # Gate: daily rate limit (persistent via heartbeat_log table)
    if get_daily_heartbeat_count(db, contractor.id) >= max_daily:
        return None

    # Gate: per-contractor frequency override
    freq_minutes = parse_frequency_to_minutes(contractor.heartbeat_frequency)
    if freq_minutes is not None:
        last_outbound = (
            db.query(Message.created_at)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .filter(
                Conversation.contractor_id == contractor.id,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if last_outbound is not None:
            last_ts = last_outbound[0]
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=datetime.UTC)
            now = datetime.datetime.now(datetime.UTC)
            elapsed = now - last_ts
            if elapsed < datetime.timedelta(minutes=freq_minutes):
                return None

    # Cheap checks -- skip LLM entirely if nothing is flagged
    check_result = run_cheap_checks(db, contractor)
    if not check_result.has_flags:
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="All cheap checks clean -- skipped LLM",
            priority=0,
        )

    # Something was flagged -- escalate to LLM for message composition
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
        direction=MessageDirection.OUTBOUND,
        body=action.message,
    )
    db.add(outbound)

    # Record heartbeat log for persistent rate limiting
    db.add(HeartbeatLog(contractor_id=contractor.id))
    db.commit()

    # Mark checklist items as triggered
    now = datetime.datetime.now(datetime.UTC)
    for item in check_result.due_checklist_items:
        item.last_triggered_at = now
        if item.schedule == ChecklistSchedule.ONCE:
            item.status = ChecklistStatus.COMPLETED
    if check_result.due_checklist_items:
        db.commit()

    return action


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class HeartbeatScheduler:
    """Manages the periodic heartbeat loop as an asyncio background task."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    # -- public API --

    def start(self) -> None:
        """Start the heartbeat loop (idempotent)."""
        if not settings.heartbeat_enabled:
            logger.info("Heartbeat disabled via config")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run())
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
        """Single heartbeat pass: evaluate every onboarded contractor concurrently."""
        # Use a dedicated session just to fetch the contractor list, then close it.
        listing_db: Session = SessionLocal()
        try:
            contractors = (
                listing_db.query(Contractor).filter(Contractor.onboarding_complete.is_(True)).all()
            )
            # Detach contractor objects so they can be used outside this session
            listing_db.expunge_all()
        finally:
            listing_db.close()

        if not contractors:
            return

        messaging_service = _build_messaging_service()
        semaphore = asyncio.Semaphore(settings.heartbeat_concurrency)

        async def _process_one(contractor: Contractor) -> None:
            """Process a single contractor with its own DB session."""
            async with semaphore:
                db: Session = SessionLocal()
                try:
                    await run_heartbeat_for_contractor(
                        db=db,
                        contractor=contractor,
                        messaging_service=messaging_service,
                        max_daily=settings.heartbeat_max_daily_messages,
                    )
                except Exception:
                    logger.exception("Heartbeat failed for contractor %d", contractor.id)
                finally:
                    db.close()

        results = await asyncio.gather(
            *[_process_one(c) for c in contractors],
            return_exceptions=True,
        )

        # Log any unexpected exceptions that escaped the per-contractor handler
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Unhandled error in heartbeat for contractor %d: %s",
                    contractors[i].id,
                    result,
                    exc_info=result if isinstance(result, Exception) else None,
                )


# Module-level singleton used by main.py lifespan
heartbeat_scheduler = HeartbeatScheduler()
