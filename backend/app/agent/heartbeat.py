"""Two-phase proactive heartbeat engine.

Phase 1 (Decision): A lightweight LLM call evaluates the user's heartbeat
items, memory, recent messages, and current time, then decides whether any
tasks need attention.  Uses a single ``heartbeat_decision`` tool that returns
``skip`` or ``run`` plus a natural-language task description.

Phase 2 (Execution): When Phase 1 returns ``run``, the task description is
handed to a full ``ClawboltAgent`` with all registered tools (QuickBooks,
file I/O, memory, etc.).  The agent executes the tasks autonomously and
produces a reply that is delivered to the user.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.dto import HeartbeatLogEntry
from backend.app.agent.file_store import HeartbeatStore
from backend.app.agent.llm_parsing import get_response_text, parse_tool_calls
from backend.app.agent.session_db import get_session_store
from backend.app.agent.system_prompt import (
    build_heartbeat_system_prompt,
    build_time_user_context,
    to_local_time,
)
from backend.app.agent.tools.names import ToolName
from backend.app.channels import get_channel, get_default_channel, get_manager
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import ChannelRoute, User
from backend.app.services.llm_service import reasoning_effort_to_thinking
from backend.app.services.llm_usage import log_llm_usage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frequency parsing
# ---------------------------------------------------------------------------

_FREQ_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)

_NAMED_FREQUENCIES: dict[str, int] = {
    "daily": 1440,
    "weekdays": 1440,
    "weekly": 10080,
}

# Minimum tick resolution: the scheduler wakes up this often.
_TICK_RESOLUTION_MINUTES = 1

# How far back to look when building heartbeat history context for the LLM.
_HISTORY_LOOKBACK_DAYS = 7


def parse_frequency_to_minutes(freq: str) -> int | None:
    """Convert a frequency string like ``15m``, ``2h``, ``1d`` to minutes.

    Named presets (``daily``, ``weekdays``, ``weekly``) are also supported.
    Returns *None* if the string cannot be parsed.
    """
    freq = freq.strip().lower()
    if freq in _NAMED_FREQUENCIES:
        return _NAMED_FREQUENCIES[freq]
    m = _FREQ_RE.match(freq)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return max(value, 1)
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    return None  # pragma: no cover


# ---------------------------------------------------------------------------
# Data structures -- Phase 1 (decision)
# ---------------------------------------------------------------------------


class HeartbeatDecisionParams(BaseModel):
    """Parameters for the Phase 1 heartbeat_decision tool."""

    action: Literal["skip", "run"]
    tasks: str = Field(
        default="",
        description=(
            "When action is 'run': a natural-language description of the tasks "
            "the agent should execute. Be specific about what to check or do."
        ),
    )
    reasoning: str = Field(description="Brief explanation of why this action was chosen")


HEARTBEAT_DECISION_TOOL: dict[str, Any] = {
    "name": ToolName.HEARTBEAT_DECISION,
    "description": (
        "Decide whether any heartbeat items or proactive tasks need attention right now. "
        "Choose 'skip' if nothing needs doing, or 'run' with a task description "
        "to hand off to the full agent for execution."
    ),
    "input_schema": HeartbeatDecisionParams.model_json_schema(),
}


@dataclass
class HeartbeatDecision:
    """Result of Phase 1: should the agent act?"""

    action: str  # "skip" or "run"
    tasks: str
    reasoning: str


# Legacy data structure kept for backwards compatibility with existing code
# that references HeartbeatAction (e.g. tests, return types).
class ComposeMessageParams(BaseModel):
    """Parameters for the heartbeat compose_message tool (legacy)."""

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


@dataclass
class HeartbeatAction:
    action_type: str  # "send_message" or "no_action"
    message: str
    reasoning: str
    priority: int


# ---------------------------------------------------------------------------
# Business-hours gate
# ---------------------------------------------------------------------------


def is_within_business_hours(
    user: User,
    now: datetime.datetime | None = None,
) -> bool:
    """Return *True* if *now* falls outside the quiet-hours window."""
    now = now or datetime.datetime.now(datetime.UTC)
    local_now = to_local_time(now, user.timezone)
    current_hour = local_now.hour

    qstart = settings.heartbeat_quiet_hours_start
    qend = settings.heartbeat_quiet_hours_end
    if qstart > qend:
        # Quiet hours span midnight (e.g. 20-7)
        in_quiet = current_hour >= qstart or current_hour < qend
    else:
        in_quiet = qstart <= current_hour < qend
    return not in_quiet


# ---------------------------------------------------------------------------
# Phase 1: Decision LLM call
# ---------------------------------------------------------------------------


def _parse_decision_response(response: MessageResponse) -> HeartbeatDecision:
    """Extract a HeartbeatDecision from the Phase 1 LLM response."""
    parsed = parse_tool_calls(response)

    if not parsed:
        content = get_response_text(response)
        logger.warning(
            "Heartbeat decision LLM returned text instead of tool call: %s", content[:200]
        )
        return HeartbeatDecision(
            action="skip", tasks="", reasoning=f"LLM did not call tool: {content[:100]}"
        )

    tc = parsed[0]
    if tc.name != ToolName.HEARTBEAT_DECISION:
        logger.warning("Heartbeat decision LLM called unexpected tool: %s", tc.name)
        return HeartbeatDecision(action="skip", tasks="", reasoning="LLM called unexpected tool")

    if tc.arguments is None:
        logger.warning("Heartbeat decision tool call had malformed arguments")
        return HeartbeatDecision(action="skip", tasks="", reasoning="Malformed tool arguments")

    try:
        params = HeartbeatDecisionParams.model_validate(tc.arguments)
    except ValidationError as exc:
        logger.warning("Heartbeat decision tool call failed validation: %s", exc)
        return HeartbeatDecision(
            action="skip", tasks="", reasoning="Tool arguments failed validation"
        )

    return HeartbeatDecision(
        action=params.action,
        tasks=params.tasks,
        reasoning=params.reasoning,
    )


# Keep legacy parser for backwards compatibility in tests
def _parse_tool_call_response(response: MessageResponse) -> HeartbeatAction:
    """Extract a HeartbeatAction from an LLM tool call response (legacy)."""
    parsed = parse_tool_calls(response)

    if not parsed:
        content = get_response_text(response)
        logger.warning("Heartbeat LLM returned text instead of tool call: %s", content[:200])
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning=f"LLM did not call compose_message tool: {content[:100]}",
            priority=0,
        )

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


def _format_heartbeat_history(
    logs: list[HeartbeatLogEntry],
    tz_name: str,
    now: datetime.datetime,
) -> str:
    """Format recent heartbeat log entries into a human-readable summary.

    Shows when heartbeat messages were previously sent so the Phase 1
    evaluator can avoid duplicate sends and reason about timing.
    """
    if not logs:
        return (
            f"You have not sent any heartbeat messages to this user "
            f"in the last {_HISTORY_LOOKBACK_DAYS} days."
        )

    lines: list[str] = []
    for entry in logs:
        ts = datetime.datetime.fromisoformat(entry.created_at)
        local_ts = to_local_time(ts, tz_name)
        formatted = local_ts.strftime("%A, %Y-%m-%d %I:%M %p").strip()
        delta = now - ts
        if delta.days == 0:
            ago = "today"
        elif delta.days == 1:
            ago = "1 day ago"
        else:
            ago = f"{delta.days} days ago"
        detail = ""
        if entry.action_type == "skip":
            detail = " [skipped]"
        elif entry.tasks:
            # Truncate long task descriptions to keep the prompt concise.
            task_preview = entry.tasks[:120]
            if len(entry.tasks) > 120:
                task_preview += "..."
            detail = f' | tasks: "{task_preview}"'
        lines.append(f"- {formatted} ({ago}){detail}")

    summary = "\n".join(lines)
    return (
        f"Your recent heartbeat messages to this user "
        f"(last {_HISTORY_LOOKBACK_DAYS} days):\n{summary}"
    )


async def evaluate_heartbeat_need(
    user: User,
    channel: str = "",
    chat_id: str = "",
) -> HeartbeatDecision:
    """Phase 1: lightweight LLM call to decide whether tasks need attention.

    The LLM sees the user's heartbeat items, memory, recent messages, and
    current time, then decides whether to skip or hand off tasks to the full
    agent.
    """
    session_store = get_session_store(user.id)
    recent = session_store.get_recent_messages(count=settings.heartbeat_recent_messages_count)
    recent_lines: list[str] = []
    for m in recent:
        label = "User" if m.direction == MessageDirection.INBOUND else "Assistant"
        ts_str = ""
        if m.timestamp:
            try:
                ts = datetime.datetime.fromisoformat(m.timestamp)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.UTC)
                local_ts = to_local_time(ts, user.timezone)
                ts_str = local_ts.strftime("%A %I:%M %p").strip()
            except (ValueError, TypeError):
                pass
        if ts_str:
            recent_lines.append(f"[{label}, {ts_str}] {m.body}")
        else:
            recent_lines.append(f"[{label}] {m.body}")
    recent_text = "\n".join(recent_lines) or "(no recent messages)"

    heartbeat_store = HeartbeatStore(user.id)
    heartbeat_md = heartbeat_store.read_heartbeat_md()

    # Fetch recent heartbeat send history so the LLM knows when it last
    # messaged this user and can avoid duplicates or missed sends.
    now = datetime.datetime.now(datetime.UTC)
    since = now - datetime.timedelta(days=_HISTORY_LOOKBACK_DAYS)
    recent_logs = await heartbeat_store.get_recent_logs(since)
    heartbeat_history = _format_heartbeat_history(recent_logs, user.timezone, now)

    prompt = await build_heartbeat_system_prompt(
        user, recent_text, heartbeat_md=heartbeat_md, heartbeat_history=heartbeat_history
    )

    logger.debug(
        "Heartbeat Phase 1 context for user %s: recent_messages=%d, "
        "heartbeat_length=%d, system_prompt_length=%d",
        user.id,
        len(recent),
        len(heartbeat_md),
        len(prompt),
    )

    # Send typing indicator before LLM call via the bus
    if channel and chat_id:
        try:
            from backend.app.bus import OutboundMessage, message_bus

            await message_bus.publish_outbound(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content="",
                    is_typing_indicator=True,
                )
            )
        except Exception:
            logger.debug("Failed to send heartbeat typing indicator to %s", chat_id)

    model = settings.heartbeat_model or settings.llm_model
    provider = settings.heartbeat_provider or settings.llm_provider

    time_context = build_time_user_context(user)
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
                    "content": (
                        f"{time_context}\n\n"
                        "Review the context above and decide whether any tasks need attention."
                    ),
                },
            ],
            tools=[HEARTBEAT_DECISION_TOOL],
            max_tokens=settings.llm_max_tokens_heartbeat,
            thinking=reasoning_effort_to_thinking(settings.reasoning_effort),
        ),
    )

    log_llm_usage(user.id, model, response, "heartbeat_decision")
    logger.debug(
        "Heartbeat Phase 1 response for user %s: stop_reason=%s, blocks=%d",
        user.id,
        getattr(response, "stop_reason", "unknown"),
        len(response.content),
    )
    return _parse_decision_response(response)


# ---------------------------------------------------------------------------
# Phase 2: Full agent execution
# ---------------------------------------------------------------------------


async def execute_heartbeat_tasks(
    user: User,
    tasks: str,
    channel: str = "",
    chat_id: str = "",
) -> str:
    """Phase 2: run the task description through the full agent loop.

    Creates a ``ClawboltAgent`` with all registered tools and processes the
    task description as if it were a user message.  Returns the agent's
    reply text, or an empty string if the agent produced no output.
    """
    from backend.app.agent.core import AgentResponse, ClawboltAgent
    from backend.app.agent.tools.registry import (
        ToolContext,
        default_registry,
        ensure_tool_modules_imported,
    )
    from backend.app.bus import message_bus

    ensure_tool_modules_imported()

    logger.info("Heartbeat Phase 2 starting for user %s: %.200s", user.id, tasks)

    publish_outbound = message_bus.publish_outbound if channel else None

    # Initialize storage backend
    storage = None
    try:
        from backend.app.agent.router import init_storage

        storage = init_storage(user)
    except Exception:
        logger.debug("Heartbeat Phase 2: storage not available for user %s", user.id)

    tool_context = ToolContext(
        user=user,
        storage=storage,
        publish_outbound=publish_outbound,
        channel=channel,
        to_address=chat_id,
    )

    agent = ClawboltAgent(
        user=user,
        channel=channel,
        publish_outbound=publish_outbound,
        chat_id=chat_id,
        tool_context=tool_context,
        registry=default_registry,
    )

    # Register ALL tools except messaging (send_reply, send_media_reply).
    # The heartbeat system delivers the agent's final reply text, so the
    # agent should not try to message the user directly.
    all_factories = set(default_registry.factory_names)
    all_factories.discard("messaging")
    tools = default_registry.create_tools(tool_context, selected_factories=all_factories)
    agent.register_tools(tools)

    logger.debug(
        "Heartbeat Phase 2 agent for user %s initialized with %d tools",
        user.id,
        len(tools),
    )

    # Use at least 1024 tokens for heartbeat execution so the agent can
    # compose detailed reports (the default agent max_tokens may be lower).
    heartbeat_max_tokens = max(settings.llm_max_tokens_agent, 1024)

    try:
        response: AgentResponse = await agent.process_message(
            message_context=tasks,
            max_tokens=heartbeat_max_tokens,
        )
    except Exception:
        logger.exception("Heartbeat Phase 2 agent failed for user %s", user.id)
        return ""

    if response.is_error_fallback:
        logger.warning("Heartbeat Phase 2 agent returned error fallback for user %s", user.id)
        return ""

    logger.info(
        "Heartbeat Phase 2 completed for user %s: reply_length=%d, actions=%s",
        user.id,
        len(response.reply_text),
        response.actions_taken or "(none)",
    )

    return response.reply_text


# ---------------------------------------------------------------------------
# Persistent rate limiting
# ---------------------------------------------------------------------------


async def get_daily_heartbeat_count(user_id: str) -> int:
    """Count heartbeat messages sent to a user today (UTC)."""
    heartbeat_store = HeartbeatStore(user_id)
    return await heartbeat_store.get_daily_count()


# ---------------------------------------------------------------------------
# Per-user runner (orchestrates Phase 1 + Phase 2)
# ---------------------------------------------------------------------------


async def run_heartbeat_for_user(
    user: User,
    channel: str,
    chat_id: str,
    max_daily: int,
) -> HeartbeatAction | None:
    """Full two-phase heartbeat pipeline for a single user.

    Phase 1: Lightweight LLM decides whether tasks need attention.
    Phase 2: Full agent executes tasks and produces a message.

    Returns a ``HeartbeatAction`` for compatibility, or *None* if skipped.
    """
    # Gate: onboarding must be complete
    if not user.onboarding_complete:
        logger.debug("Heartbeat skip user %s: onboarding not complete", user.id)
        return None

    # Gate: user heartbeat opt-in
    if not user.heartbeat_opt_in:
        logger.debug("Heartbeat skip user %s: heartbeat not opted in", user.id)
        return None

    # Gate: daily rate limit (persistent via heartbeat log)
    daily_count = await get_daily_heartbeat_count(user.id)
    if daily_count >= max_daily:
        logger.debug(
            "Heartbeat skip user %s: daily limit reached (%d/%d)",
            user.id,
            daily_count,
            max_daily,
        )
        return None

    logger.debug("Heartbeat evaluating user %s via LLM (channel=%s)", user.id, channel)

    # -- Phase 1: decide whether tasks need attention --
    decision = await evaluate_heartbeat_need(user, channel=channel, chat_id=chat_id)

    logger.debug(
        "Heartbeat Phase 1 decision for user %s: action=%s, reasoning=%s",
        user.id,
        decision.action,
        decision.reasoning,
    )

    if decision.action != "run" or not decision.tasks:
        logger.debug(
            "Heartbeat skip Phase 2 for user %s: action=%s, tasks_empty=%s",
            user.id,
            decision.action,
            not decision.tasks,
        )
        # Log the skip so the admin page shows decision history
        heartbeat_store = HeartbeatStore(user.id)
        await heartbeat_store.log_heartbeat(
            action_type="skip",
            channel=channel,
            reasoning=decision.reasoning,
        )
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning=decision.reasoning,
            priority=0,
        )

    # -- Phase 2: execute tasks via full agent loop --
    reply_text = await execute_heartbeat_tasks(
        user, decision.tasks, channel=channel, chat_id=chat_id
    )

    if not reply_text:
        logger.debug("Heartbeat Phase 2 produced no output for user %s", user.id)
        return HeartbeatAction(
            action_type="no_action",
            message="",
            reasoning="Phase 2 agent produced no output",
            priority=0,
        )

    # -- Deliver the agent's reply to the user --
    logger.info(
        "Heartbeat sending message to user %s: %.100s",
        user.id,
        reply_text,
    )
    try:
        from backend.app.bus import OutboundMessage, message_bus

        await message_bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=reply_text,
            )
        )
    except Exception:
        logger.exception("Heartbeat message failed for user %s", user.id)
        return HeartbeatAction(
            action_type="send_message",
            message=reply_text,
            reasoning=decision.reasoning,
            priority=3,
        )

    # Record outbound message in session history
    session, _ = await get_or_create_conversation(user.id)
    session_store = get_session_store(user.id)
    await session_store.add_message(
        session=session,
        direction=MessageDirection.OUTBOUND,
        body=reply_text,
    )

    # Record heartbeat log for persistent rate limiting
    heartbeat_store = HeartbeatStore(user.id)
    await heartbeat_store.log_heartbeat(
        action_type="send",
        message_text=reply_text,
        channel=channel,
        reasoning=decision.reasoning,
        tasks=decision.tasks,
    )

    return HeartbeatAction(
        action_type="send_message",
        message=reply_text,
        reasoning=decision.reasoning,
        priority=3,
    )


# ---------------------------------------------------------------------------
# Channel selection for proactive messages
# ---------------------------------------------------------------------------

# Channels that cannot deliver proactive (push) messages because the user
# must be actively connected to receive them.
_NON_PUSHABLE_CHANNELS: frozenset[str] = frozenset({"webchat"})


def _pick_heartbeat_channel(user: User) -> str:
    """Select the best channel name for delivering a heartbeat message.

    Prefers the user's ``preferred_channel`` when it can actually push
    messages.  When the preferred channel is non-pushable (e.g. webchat),
    falls back to the first registered pushable channel.  If no pushable
    channel is available at all, returns the default channel's name as a
    last resort (matching the previous behavior).
    """
    preferred = user.preferred_channel

    # Happy path: preferred channel is pushable
    if preferred not in _NON_PUSHABLE_CHANNELS:
        try:
            get_channel(preferred)
            logger.debug(
                "Heartbeat for user %s: using preferred channel %r",
                user.id,
                preferred,
            )
            return preferred
        except KeyError:
            logger.debug(
                "Heartbeat for user %s: preferred channel %r not registered, searching fallbacks",
                user.id,
                preferred,
            )

    # Preferred channel is non-pushable or not registered: find the
    # first registered channel that can deliver proactive messages.
    manager = get_manager()
    for name in manager.channels:
        if name not in _NON_PUSHABLE_CHANNELS:
            logger.debug(
                "Heartbeat for user %s: preferred channel %r is non-pushable, falling back to %r",
                user.id,
                preferred,
                name,
            )
            return name

    # No pushable channels registered at all: fall back to default
    logger.warning(
        "Heartbeat for user %s: no pushable channels registered, using default channel",
        user.id,
    )
    return get_default_channel().name


def resolve_heartbeat_route(
    user: User,
    db: Session,
) -> tuple[str, ChannelRoute] | None:
    """Pick the best channel for *user* and find a matching route.

    Returns ``(channel_name, route)`` on success, or ``None`` when no
    pushable route can be found.  The caller supplies an open DB session;
    this function performs read-only queries.

    Resolution order:

    1. The channel selected by ``_pick_heartbeat_channel`` (preferred or
       first registered pushable channel).
    2. If no route exists for that channel, iterate all of the user's
       routes and return the first one whose channel is pushable and
       registered.
    """
    channel_name = _pick_heartbeat_channel(user)

    route = db.query(ChannelRoute).filter_by(user_id=user.id, channel=channel_name).first()

    if route is not None and route.enabled:
        return channel_name, route

    # Fallback: try any other pushable channel route the user has.
    routes = db.query(ChannelRoute).filter_by(user_id=user.id).all()
    route_channels = [r.channel for r in routes]
    logger.debug(
        "Heartbeat for user %s: no %s route, searching %d route(s): %s",
        user.id,
        channel_name,
        len(routes),
        route_channels,
    )
    for r in routes:
        if r.channel not in _NON_PUSHABLE_CHANNELS and r.enabled:
            try:
                get_channel(r.channel)
            except KeyError:
                continue
            logger.debug(
                "Heartbeat for user %s: fell back to %s route",
                user.id,
                r.channel,
            )
            return r.channel, r

    logger.debug(
        "Heartbeat skipped for user %s: no pushable route configured",
        user.id,
    )
    return None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def user_interval_minutes(user: User) -> int:
    """Return the heartbeat interval in minutes for a given user.

    Parses the user's ``heartbeat_frequency`` string (e.g. "30m", "2h"),
    falling back to the global ``settings.heartbeat_interval_minutes``.
    """
    parsed = parse_frequency_to_minutes(user.heartbeat_frequency)
    if parsed is not None:
        return parsed
    return settings.heartbeat_interval_minutes


def is_user_due(
    user: User,
    now: datetime.datetime,
    last_tick: dict[str, datetime.datetime],
) -> bool:
    """Return True if enough time has elapsed since the last tick for this user.

    ``last_tick`` maps user IDs to the last evaluation timestamp.
    """
    last = last_tick.get(user.id)
    if last is None:
        return True
    interval = user_interval_minutes(user)
    return (now - last).total_seconds() >= interval * 60


class HeartbeatScheduler:
    """Manages the periodic heartbeat loop as an asyncio background task.

    The scheduler wakes up every ``_TICK_RESOLUTION_MINUTES`` and evaluates
    each user only when their individual ``heartbeat_frequency`` interval has
    elapsed since their last evaluation.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._last_tick: dict[str, datetime.datetime] = {}

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
            "Heartbeat started (tick_resolution=%dm, max_daily=%d)",
            _TICK_RESOLUTION_MINUTES,
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
        """Loop forever, running one tick per resolution interval."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heartbeat tick failed")
            await asyncio.sleep(_TICK_RESOLUTION_MINUTES * 60)

    @staticmethod
    def _user_interval_minutes(user: User) -> int:
        """Return the heartbeat interval in minutes for a given user."""
        return user_interval_minutes(user)

    def _is_user_due(self, user: User, now: datetime.datetime) -> bool:
        """Return True if enough time has elapsed since the last tick for this user."""
        return is_user_due(user, now, self._last_tick)

    async def tick(self) -> None:
        """Single heartbeat pass: evaluate due users concurrently."""
        logger.debug("Heartbeat tick starting")
        db = SessionLocal()
        try:
            all_users = db.query(User).all()
            for u in all_users:
                db.expunge(u)
        finally:
            db.close()
        users = [c for c in all_users if c.onboarding_complete and c.is_active]

        if not users:
            logger.debug("Heartbeat tick: no onboarded users found")
            return

        now = datetime.datetime.now(datetime.UTC)
        due_users = [u for u in users if self._is_user_due(u, now)]

        if not due_users:
            logger.debug(
                "Heartbeat tick: %d onboarded user(s) but none due yet",
                len(users),
            )
            return

        logger.info(
            "Heartbeat tick: evaluating %d/%d user(s)",
            len(due_users),
            len(users),
        )

        semaphore = asyncio.Semaphore(settings.heartbeat_concurrency)

        async def _process_one(user: User) -> None:
            """Process a single user."""
            async with semaphore:
                try:
                    db = SessionLocal()
                    try:
                        result = resolve_heartbeat_route(user, db)
                    finally:
                        db.close()

                    if result is None:
                        return

                    channel_name, route = result
                    max_daily = (
                        user.heartbeat_max_daily
                        if user.heartbeat_max_daily > 0
                        else settings.heartbeat_max_daily_messages
                    )
                    await run_heartbeat_for_user(
                        user=user,
                        channel=channel_name,
                        chat_id=route.channel_identifier,
                        max_daily=max_daily,
                    )
                    self._last_tick[user.id] = now
                except Exception:
                    logger.exception("Heartbeat failed for user %s", user.id)

        results = await asyncio.gather(
            *[_process_one(c) for c in due_users],
            return_exceptions=True,
        )

        # Log any unexpected exceptions that escaped the per-user handler
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Unhandled error in heartbeat for user %s: %s",
                    due_users[i].id,
                    result,
                    exc_info=result if isinstance(result, Exception) else None,
                )


# Module-level singleton used by main.py lifespan
heartbeat_scheduler = HeartbeatScheduler()
