"""Proactive heartbeat engine.

Every ``heartbeat_interval_minutes`` the scheduler wakes up, iterates over
onboarded users, and runs **cheap deterministic checks** first.  Only when
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
from typing import Any, Literal, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse
from pydantic import BaseModel, Field, ValidationError

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import (
    ChecklistItem,
    EstimateData,
    HeartbeatStore,
    MemoryFact,
    UserData,
    get_session_store,
    get_user_store,
)
from backend.app.agent.llm_parsing import get_response_text, parse_tool_calls
from backend.app.agent.system_prompt import build_heartbeat_system_prompt
from backend.app.agent.tools.names import ToolName
from backend.app.channels import get_channel, get_default_channel
from backend.app.config import settings
from backend.app.enums import (
    ChecklistSchedule,
    ChecklistStatus,
    EstimateStatus,
    MessageDirection,
)
from backend.app.services.llm_usage import log_llm_usage
from backend.app.services.messaging import MessagingService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ComposeMessageParams(BaseModel):
    """Parameters for the heartbeat compose_message tool."""

    action: Literal["send_message", "no_action"]
    message: str = Field(
        default="", description="The message to send (required if action is send_message)"
    )
    reasoning: str = Field(description="Brief explanation of why this action was chosen")
    priority: int = Field(ge=1, le=5, description="Priority level from 1 (lowest) to 5 (highest)")


COMPOSE_MESSAGE_TOOL: dict[str, Any] = {
    "name": ToolName.COMPOSE_MESSAGE,
    "description": (
        "Compose a proactive message to send to the user, or decide no message is needed."
    ),
    "input_schema": ComposeMessageParams.model_json_schema(),
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
    """Result of deterministic pre-checks for a single user."""

    flags: list[str] = field(default_factory=list)
    stale_estimates: list[EstimateData] = field(default_factory=list)
    due_checklist_items: list[ChecklistItem] = field(default_factory=list)
    time_sensitive_memories: list[MemoryFact] = field(default_factory=list)

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
    user: UserData,
    now: datetime.datetime | None = None,
) -> bool:
    """Return *True* if *now* falls within the user's business hours.

    When the user has a ``timezone`` set, *now* is converted to their
    local time before comparing against business hours or the global quiet
    hours window.  Falls back to UTC when the timezone is empty or invalid.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    local_now = _to_local_time(now, user.timezone)
    current_hour = local_now.hour

    # Use global quiet hours to determine business hours.
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


async def run_cheap_checks(
    user: UserData,
    now: datetime.datetime | None = None,
) -> CheapCheckResult:
    """Run fast, deterministic checks that don't require an LLM call.

    Returns a ``CheapCheckResult`` with flags describing what needs attention.
    If ``flags`` is empty, everything is clean and the LLM can be skipped.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    result = CheapCheckResult()

    # 1. Stale draft estimates (older than STALE_ESTIMATE_HOURS)
    from backend.app.agent.file_store import EstimateStore

    estimate_store = EstimateStore(user.id)
    all_estimates = await estimate_store.list_all()
    cutoff = now - datetime.timedelta(hours=STALE_ESTIMATE_HOURS)
    stale: list[EstimateData] = []
    for e in all_estimates:
        if e.status == EstimateStatus.DRAFT:
            try:
                created = datetime.datetime.fromisoformat(e.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.UTC)
                if created <= cutoff:
                    stale.append(e)
            except (ValueError, TypeError):
                pass
    if stale:
        result.stale_estimates = stale
        descs = ", ".join(e.description[:40] for e in stale)
        result.flags.append(f"Stale draft estimate(s) older than 24h: {descs}")

    # 2. Due checklist items
    heartbeat_store = HeartbeatStore(user.id)
    all_items = await heartbeat_store.get_checklist()
    for item in all_items:
        if item.status != ChecklistStatus.ACTIVE:
            continue
        if _is_checklist_item_due(item, now, tz_name=user.timezone or ""):
            result.due_checklist_items.append(item)
            result.flags.append(f"Checklist item due: {item.description}")

    # 3. Time-sensitive memory facts
    from backend.app.agent.file_store import get_memory_store

    memory_store = get_memory_store(user.id)
    memories = await memory_store.get_all_memories()
    for mem in memories:
        text = f"{mem.key} {mem.value}"
        if _TIME_KEYWORDS.search(text):
            result.time_sensitive_memories.append(mem)
            result.flags.append(f"Time-sensitive memory: {mem.key} = {mem.value}")

    # 4. Idle user -- no inbound messages for IDLE_DAYS
    idle_cutoff = now - datetime.timedelta(days=IDLE_DAYS)
    session_store = get_session_store(user.id)
    last_inbound = session_store.get_last_inbound_timestamp()
    if last_inbound is not None:
        if last_inbound <= idle_cutoff:
            days = (now - last_inbound).days
            result.flags.append(f"User idle for {days} days -- no recent messages")
    elif user.created_at is not None:
        created = user.created_at
        if isinstance(created, str):
            try:
                created = datetime.datetime.fromisoformat(created)
            except (ValueError, TypeError):
                created = None
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.UTC)
            if created <= idle_cutoff:
                days = (now - created).days
                result.flags.append(f"User idle for {days} days -- no messages since onboarding")

    return result


def _is_checklist_item_due(
    item: ChecklistItem,
    now: datetime.datetime,
    tz_name: str = "",
) -> bool:
    """Determine whether a checklist item should fire on this tick."""
    # Weekday gate: convert to user's local timezone so that
    # e.g. Friday 5 PM Pacific is not treated as Saturday (UTC).
    local_now = _to_local_time(now, tz_name) if tz_name else now
    if item.schedule == ChecklistSchedule.WEEKDAYS and local_now.weekday() > WEEKDAY_FRIDAY:
        return False

    # Never triggered -> due (for daily/weekdays/once)
    if item.last_triggered_at is None:
        return True

    last_str = item.last_triggered_at
    try:
        last = datetime.datetime.fromisoformat(last_str)
    except (ValueError, TypeError):
        return True
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


def _load_recent_messages(user: UserData) -> str:
    """Load recent messages as formatted text for heartbeat context."""
    session_store = get_session_store(user.id)
    recent = session_store.get_recent_messages(count=HEARTBEAT_RECENT_MESSAGES_COUNT)
    if not recent:
        return "(no recent messages)"

    lines: list[str] = []
    for msg in recent:
        direction = "User" if msg.direction == MessageDirection.INBOUND else "Assistant"
        lines.append(f"[{direction}] {msg.body}")
    return "\n".join(lines) or "(no recent messages)"


async def build_heartbeat_context(
    user: UserData,
    flags: list[str],
) -> str:
    """Build the full heartbeat system prompt via the composable builder."""
    recent_messages = _load_recent_messages(user)
    return await build_heartbeat_system_prompt(user, flags, recent_messages)


# ---------------------------------------------------------------------------
# Tool call response parsing
# ---------------------------------------------------------------------------


def _parse_tool_call_response(response: MessageResponse) -> HeartbeatAction:
    """Extract a HeartbeatAction from an LLM tool call response.

    If the LLM did not call the compose_message tool (e.g. returned plain text
    instead), falls back to no_action.
    """
    parsed = parse_tool_calls(response)

    if not parsed:
        # LLM returned text instead of calling the tool: default to no_action
        content = get_response_text(response)
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

    try:
        params = ComposeMessageParams.model_validate(tc.arguments)
    except ValidationError as exc:
        logger.warning("Heartbeat tool call failed validation: %s", exc)
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="Tool arguments failed validation",
            priority=0,
        )

    return HeartbeatAction(
        action_type=params.action,
        message=params.message,
        reasoning=params.reasoning,
        priority=params.priority,
    )


# ---------------------------------------------------------------------------
# LLM evaluation (only called when cheap checks flag something)
# ---------------------------------------------------------------------------


async def evaluate_heartbeat_need(
    user: UserData,
    flags: list[str],
    messaging_service: MessagingService | None = None,
) -> HeartbeatAction:
    """Ask the LLM to compose a message based on flagged items.

    Uses the compose_message tool calling protocol instead of raw JSON parsing.
    If the LLM does not call the tool, defaults to no_action.
    Sends a typing indicator before the LLM call when a messaging_service is provided.
    """
    prompt = await build_heartbeat_context(user, flags)

    # Send typing indicator before LLM call
    if messaging_service:
        to_address = user.channel_identifier or user.phone
        if to_address:
            try:
                await messaging_service.send_typing_indicator(to=to_address)
            except Exception:
                logger.debug("Failed to send heartbeat typing indicator to %s", to_address)

    model = settings.heartbeat_model or settings.llm_model
    provider = settings.heartbeat_provider or settings.llm_provider

    response = cast(
        MessageResponse,
        await amessages(
            model=model,
            provider=provider,
            api_base=settings.llm_api_base,
            system=prompt,
            messages=[
                {
                    "role": "user",
                    "content": "Compose a proactive message based on the flags above.",
                },
            ],
            tools=[COMPOSE_MESSAGE_TOOL],
            max_tokens=settings.llm_max_tokens_heartbeat,
        ),
    )

    log_llm_usage(user.id, model, response, "heartbeat")
    return _parse_tool_call_response(response)


# ---------------------------------------------------------------------------
# Persistent rate limiting
# ---------------------------------------------------------------------------


async def get_daily_heartbeat_count(user_id: int) -> int:
    """Count heartbeat messages sent to a user today (UTC).

    Queries the heartbeat log instead of relying on in-memory state
    so that rate limits survive process restarts and work across multiple
    workers.
    """
    heartbeat_store = HeartbeatStore(user_id)
    return await heartbeat_store.get_daily_count()


# ---------------------------------------------------------------------------
# Per-user runner
# ---------------------------------------------------------------------------


async def run_heartbeat_for_user(
    user: UserData,
    messaging_service: MessagingService,
    max_daily: int,
) -> HeartbeatAction | None:
    """Full heartbeat pipeline for a single user.

    Returns the action taken, or *None* if skipped.
    """
    # Gate: onboarding must be complete
    if not user.onboarding_complete:
        return None

    # Gate: user heartbeat opt-in
    if not user.heartbeat_opt_in:
        return None

    # Gate: business hours
    if not is_within_business_hours(user):
        return None

    # Gate: daily rate limit (persistent via heartbeat log)
    if await get_daily_heartbeat_count(user.id) >= max_daily:
        return None

    # Gate: per-user frequency override
    freq_minutes = parse_frequency_to_minutes(user.heartbeat_frequency)
    if freq_minutes is not None:
        session_store = get_session_store(user.id)
        last_outbound = session_store.get_last_outbound_timestamp()
        if last_outbound is not None:
            now = datetime.datetime.now(datetime.UTC)
            elapsed = now - last_outbound
            if elapsed < datetime.timedelta(minutes=freq_minutes):
                return None

    # Cheap checks -- skip LLM entirely if nothing is flagged
    check_result = await run_cheap_checks(user)
    if not check_result.has_flags:
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="All cheap checks clean -- skipped LLM",
            priority=0,
        )

    # Something was flagged -- escalate to LLM for message composition
    action = await evaluate_heartbeat_need(
        user, check_result.flags, messaging_service=messaging_service
    )

    if action.action_type != "send_message" or not action.message:
        return action

    # Send message
    to_address = user.channel_identifier or user.phone
    try:
        await messaging_service.send_text(to=to_address, body=action.message)
    except Exception:
        logger.exception("Heartbeat message failed for user %d", user.id)
        return action

    # Record outbound message
    session, _ = await get_or_create_conversation(user.id)
    session_store = get_session_store(user.id)
    await session_store.add_message(
        session=session,
        direction=MessageDirection.OUTBOUND,
        body=action.message,
    )

    # Record heartbeat log for persistent rate limiting
    heartbeat_store = HeartbeatStore(user.id)
    await heartbeat_store.log_heartbeat()

    # Mark checklist items as triggered
    now = datetime.datetime.now(datetime.UTC)
    for item in check_result.due_checklist_items:
        updates: dict[str, Any] = {"last_triggered_at": now.isoformat()}
        if item.schedule == ChecklistSchedule.ONCE:
            updates["status"] = ChecklistStatus.COMPLETED
        await heartbeat_store.update_checklist_item(item.id, **updates)

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
        """Single heartbeat pass: evaluate every onboarded user concurrently."""
        store = get_user_store()
        all_users = await store.list_all()
        users = [c for c in all_users if c.onboarding_complete]

        if not users:
            return

        semaphore = asyncio.Semaphore(settings.heartbeat_concurrency)

        async def _process_one(user: UserData) -> None:
            """Process a single user."""
            async with semaphore:
                try:
                    # Route to the user's preferred channel, falling
                    # back to the first registered channel.
                    try:
                        messaging_service: MessagingService = get_channel(user.preferred_channel)
                    except KeyError:
                        messaging_service = get_default_channel()

                    await run_heartbeat_for_user(
                        user=user,
                        messaging_service=messaging_service,
                        max_daily=settings.heartbeat_max_daily_messages,
                    )
                except Exception:
                    logger.exception("Heartbeat failed for user %d", user.id)

        results = await asyncio.gather(
            *[_process_one(c) for c in users],
            return_exceptions=True,
        )

        # Log any unexpected exceptions that escaped the per-user handler
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Unhandled error in heartbeat for user %d: %s",
                    users[i].id,
                    result,
                    exc_info=result if isinstance(result, Exception) else None,
                )


# Module-level singleton used by main.py lifespan
heartbeat_scheduler = HeartbeatScheduler()
