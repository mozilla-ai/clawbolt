"""Proactive heartbeat engine.

Every ``heartbeat_interval_minutes`` the scheduler wakes up, iterates over
onboarded users, and makes a single LLM call per user to decide whether
a proactive message is needed.  The LLM sees the user's checklist, memory,
recent messages, and current time, then decides holistically whether to
reach out.
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

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import (
    HeartbeatStore,
    UserData,
    get_session_store,
    get_user_store,
)
from backend.app.agent.llm_parsing import get_response_text, parse_tool_calls
from backend.app.agent.system_prompt import build_heartbeat_system_prompt, to_local_time
from backend.app.agent.tools.names import ToolName
from backend.app.channels import get_channel, get_default_channel, get_manager
from backend.app.config import settings
from backend.app.enums import MessageDirection
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
    user: UserData,
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
# LLM evaluation
# ---------------------------------------------------------------------------


async def evaluate_heartbeat_need(
    user: UserData,
    channel: str = "",
    chat_id: str = "",
) -> HeartbeatAction:
    """Single LLM call to evaluate whether a proactive message is needed.

    The LLM sees the user's checklist, memory, recent messages, and current
    time, and decides holistically whether to send a message.
    """
    session_store = get_session_store(user.id)
    recent = session_store.get_recent_messages(count=settings.heartbeat_recent_messages_count)
    recent_text = (
        "\n".join(
            f"[{'User' if m.direction == MessageDirection.INBOUND else 'Assistant'}] {m.body}"
            for m in recent
        )
        or "(no recent messages)"
    )

    heartbeat_store = HeartbeatStore(user.id)
    checklist_md = heartbeat_store.read_checklist_md()

    prompt = await build_heartbeat_system_prompt(user, recent_text, checklist_md=checklist_md)

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

    logger.debug(
        "Heartbeat LLM call for user %d: model=%s, provider=%s",
        user.id,
        model,
        provider,
    )

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
                        "Review the context above and decide whether to send a proactive message."
                    ),
                },
            ],
            tools=[COMPOSE_MESSAGE_TOOL],
            max_tokens=settings.llm_max_tokens_heartbeat,
        ),
    )

    log_llm_usage(user.id, model, response, "heartbeat")
    logger.debug(
        "Heartbeat LLM raw response for user %d: stop_reason=%s, content_blocks=%d",
        user.id,
        getattr(response, "stop_reason", "unknown"),
        len(response.content),
    )
    return _parse_tool_call_response(response)


# ---------------------------------------------------------------------------
# Persistent rate limiting
# ---------------------------------------------------------------------------


async def get_daily_heartbeat_count(user_id: int) -> int:
    """Count heartbeat messages sent to a user today (UTC)."""
    heartbeat_store = HeartbeatStore(user_id)
    return await heartbeat_store.get_daily_count()


# ---------------------------------------------------------------------------
# Per-user runner
# ---------------------------------------------------------------------------


async def run_heartbeat_for_user(
    user: UserData,
    channel: str,
    chat_id: str,
    max_daily: int,
) -> HeartbeatAction | None:
    """Full heartbeat pipeline for a single user.

    Returns the action taken, or *None* if skipped.
    """
    # Gate: onboarding must be complete
    if not user.onboarding_complete:
        logger.debug("Heartbeat skip user %d: onboarding not complete", user.id)
        return None

    # Gate: user heartbeat opt-in
    if not user.heartbeat_opt_in:
        logger.debug("Heartbeat skip user %d: heartbeat not opted in", user.id)
        return None

    # Gate: business hours
    if not is_within_business_hours(user):
        logger.debug("Heartbeat skip user %d: outside business hours", user.id)
        return None

    # Gate: daily rate limit (persistent via heartbeat log)
    daily_count = await get_daily_heartbeat_count(user.id)
    if daily_count >= max_daily:
        logger.debug(
            "Heartbeat skip user %d: daily limit reached (%d/%d)",
            user.id,
            daily_count,
            max_daily,
        )
        return None

    logger.debug("Heartbeat evaluating user %d via LLM (channel=%s)", user.id, channel)

    # Single LLM call: the model evaluates all context holistically
    action = await evaluate_heartbeat_need(user, channel=channel, chat_id=chat_id)

    logger.debug(
        "Heartbeat LLM decision for user %d: action=%s, priority=%d, reasoning=%s",
        user.id,
        action.action_type,
        action.priority,
        action.reasoning,
    )

    if action.action_type != "send_message" or not action.message:
        logger.debug(
            "Heartbeat no message for user %d: action=%s, message_empty=%s",
            user.id,
            action.action_type,
            not action.message,
        )
        return action

    # Send message via the bus
    logger.info(
        "Heartbeat sending message to user %d (priority=%d): %.100s",
        user.id,
        action.priority,
        action.message,
    )
    try:
        from backend.app.bus import OutboundMessage, message_bus

        await message_bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=action.message,
            )
        )
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

    return action


# ---------------------------------------------------------------------------
# Channel selection for proactive messages
# ---------------------------------------------------------------------------

# Channels that cannot deliver proactive (push) messages because the user
# must be actively connected to receive them.
_NON_PUSHABLE_CHANNELS: frozenset[str] = frozenset({"webchat"})


def _pick_heartbeat_channel(user: UserData) -> str:
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
            return preferred
        except KeyError:
            pass

    # Preferred channel is non-pushable or not registered: find the
    # first registered channel that can deliver proactive messages.
    manager = get_manager()
    for name in manager.channels:
        if name not in _NON_PUSHABLE_CHANNELS:
            logger.debug(
                "Heartbeat for user %d: preferred channel %r is non-pushable, falling back to %r",
                user.id,
                preferred,
                name,
            )
            return name

    # No pushable channels registered at all: fall back to default
    logger.warning(
        "Heartbeat for user %d: no pushable channels registered, using default channel",
        user.id,
    )
    return get_default_channel().name


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class HeartbeatScheduler:
    """Manages the periodic heartbeat loop as an asyncio background task.

    The scheduler wakes up every ``_TICK_RESOLUTION_MINUTES`` and evaluates
    each user only when their individual ``heartbeat_frequency`` interval has
    elapsed since their last evaluation.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._last_tick: dict[int, datetime.datetime] = {}

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

    def _user_interval_minutes(self, user: UserData) -> int:
        """Return the heartbeat interval in minutes for a given user."""
        parsed = parse_frequency_to_minutes(user.heartbeat_frequency)
        if parsed is not None:
            return parsed
        return settings.heartbeat_interval_minutes

    def _is_user_due(self, user: UserData, now: datetime.datetime) -> bool:
        """Return True if enough time has elapsed since the last tick for this user."""
        last = self._last_tick.get(user.id)
        if last is None:
            return True
        interval = self._user_interval_minutes(user)
        return (now - last).total_seconds() >= interval * 60

    async def tick(self) -> None:
        """Single heartbeat pass: evaluate due users concurrently."""
        logger.debug("Heartbeat tick starting")
        store = get_user_store()
        all_users = await store.list_all()
        users = [c for c in all_users if c.onboarding_complete]

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

        async def _process_one(user: UserData) -> None:
            """Process a single user."""
            async with semaphore:
                try:
                    channel_name = _pick_heartbeat_channel(user)
                    chat_id = user.channel_identifier or user.phone

                    await run_heartbeat_for_user(
                        user=user,
                        channel=channel_name,
                        chat_id=chat_id,
                        max_daily=settings.heartbeat_max_daily_messages,
                    )
                    self._last_tick[user.id] = now
                except Exception:
                    logger.exception("Heartbeat failed for user %d", user.id)

        results = await asyncio.gather(
            *[_process_one(c) for c in due_users],
            return_exceptions=True,
        )

        # Log any unexpected exceptions that escaped the per-user handler
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error(
                    "Unhandled error in heartbeat for user %d: %s",
                    due_users[i].id,
                    result,
                    exc_info=result if isinstance(result, Exception) else None,
                )


# Module-level singleton used by main.py lifespan
heartbeat_scheduler = HeartbeatScheduler()
