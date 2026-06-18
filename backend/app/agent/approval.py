"""Progressive approval system for tool execution.

Provides a permission layer that lets users control what the agent can do
autonomously vs. what requires explicit approval. Tools opt in by setting
an ``approval_policy`` on their ``Tool`` definition.

Three permission levels: ALWAYS (execute freely), ASK (prompt user first),
NEVER (filter the tool out of the LLM schema entirely so it cannot be
called). Users can respond with yes/always/no/never to control both
immediate and future behavior.

Sequential approval: when a user request triggers multiple tools, each
tool that requires approval gets its own prompt. The user approves or
rejects each tool independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatch
from typing import Any, Literal, cast

from any_llm import acompletion
from any_llm.types.completion import ChatCompletion
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.app.bus import OutboundMessage
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, db_session_async
from backend.app.models import ApprovalEvent, PendingApprovalRow, UserPermissionSet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ApprovalStore helpers
# ---------------------------------------------------------------------------


def _user_permissions_lock_key(user_id: str) -> str:
    """Stable string key for the per-user permissions advisory lock."""
    return f"user_permissions:{user_id}"


async def _lock_user_permissions(db: Any, user_id: str) -> None:
    """Acquire a transaction-scoped Postgres advisory lock for this user's
    permissions row.

    Serializes concurrent read-modify-write sequences across workers and
    requests. The lock is bound to the surrounding transaction and
    released only on COMMIT / ROLLBACK of that transaction. ``db`` is
    an ``AsyncSession`` (or any handle that awaits ``execute(...)``);
    the caller must hold the lock and the matching read+write inside a
    single ``async with db_session_async()`` block so the autobegun
    transaction owns the lock.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": _user_permissions_lock_key(user_id)},
    )


def _parse_row_data(row: UserPermissionSet | None) -> dict[str, Any]:
    """Parse a UserPermissionSet.data blob into a dict, falling back to
    the default shape on missing row or malformed JSON."""
    default = {"version": _PERMISSIONS_VERSION, "tools": {}, "resources": {}}
    if row is None:
        return default
    try:
        parsed = json.loads(row.data)
    except (json.JSONDecodeError, ValueError):
        return default
    if not isinstance(parsed, dict):
        return default
    return parsed


def _select_user_permissions(user_id: str) -> Any:
    return select(UserPermissionSet).filter_by(user_id=user_id)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PermissionLevel(StrEnum):
    """Permission level for a tool or resource.

    ``ALWAYS`` runs the tool freely. ``ASK`` prompts the user before each
    invocation. ``NEVER`` filters the tool out of the LLM schema entirely
    so the agent cannot call it. ``NEVER`` is the schema-level off switch
    that replaced the legacy ``disabled_sub_tools`` column on
    ``tool_configs``: a single per-user-per-tool override now lives only
    in ``user_permissions``.
    """

    ALWAYS = "always"
    ASK = "ask"
    NEVER = "never"


class ApprovalDecision(StrEnum):
    """User's decision when prompted for approval."""

    APPROVED = "approved"
    DENIED = "denied"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_ALLOW_ALL = "always_allow_all"
    ALWAYS_DENY = "always_deny"
    INTERRUPTED = "interrupted"


# Sentinel returned by ``classify_approval_response`` when a reply is short
# filler ("lol", "haha", "ok wait") that is neither a clear yes/no nor a clear
# new request. The caller re-prompts for a clear decision instead of resolving
# the gate as INTERRUPTED, which would abort the rest of a batched approval and
# leave a multi-event action (e.g. rescheduling a multi-day job) half-applied.
AMBIGUOUS_APPROVAL_REPLY: Literal["ambiguous"] = "ambiguous"

# How many times a single pending approval may be re-prompted after ambiguous
# replies before we give up and treat the next ambiguous reply as a genuine
# interruption. Keeps a user who keeps sending filler from looping forever; the
# approval timeout is the outer bound.
_MAX_APPROVAL_REPROMPTS = 2


# ---------------------------------------------------------------------------
# ApprovalPolicy (attached to Tool definitions)
# ---------------------------------------------------------------------------


@dataclass
class ApprovalPolicy:
    """Declares how a tool participates in the approval system.

    Attributes:
        default_level: Permission level when no stored override exists.
        resource_extractor: Optional callable that extracts a resource key
            (e.g. domain from a URL) from the tool's validated arguments.
        description_builder: Optional callable that produces a human-readable
            description of what the tool call will do, shown in the approval
            prompt.
        resource_noun: Optional plural noun for the resources this tool scopes
            by (e.g. "recipients" for an email-send tool, "domains" for a web
            fetch). When set alongside ``resource_extractor``, the approval
            prompt offers an extra "always all" option that grants a blanket
            tool-level approval covering every resource, not just the one in
            front of the user. Leave ``None`` to keep approvals strictly
            per-resource.
    """

    default_level: PermissionLevel = PermissionLevel.ASK
    resource_extractor: Callable[[dict[str, Any]], str | None] | None = None
    description_builder: Callable[[dict[str, Any]], str] | None = None
    resource_noun: str | None = None


# ---------------------------------------------------------------------------
# PlanStep and plan formatting
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    """A single step in a batch approval plan.

    Attributes:
        tool_name: The tool's registered name.
        description: Human-readable description of what this step does.
        level: The resolved permission level for this step.
    """

    tool_name: str
    description: str
    level: PermissionLevel


def format_plan_message(
    plan_description: str,
    auto_steps: list[PlanStep],
    ask_steps: list[PlanStep],
) -> str:
    """Build a plain-text plan message for batch approval.

    Uses natural language to clearly separate what the agent will do
    automatically from what needs user approval.
    """
    if not ask_steps:
        return ""

    _reply_line = (
        "Reply with one of:\n"
        "  yes: allow this once\n"
        "  no: deny this once\n"
        "  always: allow and remember\n"
        "  never: deny and remember"
    )

    # Single ask, no auto: simple prompt
    if len(ask_steps) == 1 and not auto_steps:
        desc = ask_steps[0].description
        return f"I'd like to: {desc}\n\n{_reply_line}"

    # Auto steps preamble
    auto_part = ""
    if auto_steps:
        auto_desc = ", ".join(s.description.lower() for s in auto_steps)
        auto_part = f"I'll {auto_desc}."

    # Single ask with auto steps
    if len(ask_steps) == 1:
        ask_desc = ask_steps[0].description.lower()
        return f"{auto_part} I need your approval to {ask_desc}.\n\n{_reply_line}"

    # Multiple ask steps
    ask_lines = "\n".join(f"  - {step.description}" for step in ask_steps)
    parts = []
    if auto_part:
        parts.append(auto_part)
    parts.append(f"I need your approval for:\n{ask_lines}")
    parts.append("")
    parts.append(_reply_line)
    return " ".join(parts[:2]) + "\n" + "\n".join(parts[2:]) if auto_part else "\n".join(parts)


# ---------------------------------------------------------------------------
# ApprovalStore (per-user JSON persistence)
# ---------------------------------------------------------------------------

_PERMISSIONS_VERSION = 1


class ApprovalStore:
    """Persists per-user tool permission overrides.

    Storage format (``permissions.json``)::

        {
            "version": 1,
            "tools": {"web_search": "always", "bash_exec": "never"},
            "resources": {
                "web_fetch": {"homedepot.com": "always", "*.gov": "always"}
            }
        }

    Resolution order: resource match (exact then glob) > tool match > policy default.
    """

    async def _load(self, user_id: str) -> dict[str, Any]:
        async with db_session_async() as db:
            row = (await db.execute(_select_user_permissions(user_id))).scalar_one_or_none()
            return _parse_row_data(row)

    async def _save(self, user_id: str, data: dict[str, Any]) -> None:
        """Wholesale replace. Serialized against concurrent writers via an
        advisory lock keyed on the user so the dashboard PUT can't race
        with set_permission or a workspace_tools write on the same row.

        Acquires the per-user ``pg_advisory_xact_lock`` on the same
        AsyncSession that performs the read+write. The lock and the
        DML share one autobegun transaction, so the lock is released
        only when ``await db.commit()`` (handled by
        ``db_session_async``) ends that transaction.
        """
        payload = json.dumps(data, indent=2, default=str)
        async with db_session_async() as db:
            await _lock_user_permissions(db, user_id)
            row = (await db.execute(_select_user_permissions(user_id))).scalar_one_or_none()
            if row is None:
                db.add(UserPermissionSet(user_id=user_id, data=payload))
            else:
                row.data = payload
            await db.commit()

    async def load_user_permissions(self, user_id: str) -> dict[str, Any]:
        """Load the raw permission data for a user.

        Use with :meth:`resolve_permission` for bulk lookups to avoid
        repeated DB reads.
        """
        return await self._load(user_id)

    @staticmethod
    def resolve_permission(
        data: dict[str, Any],
        tool_name: str,
        resource: str | None = None,
        default: PermissionLevel = PermissionLevel.ASK,
    ) -> PermissionLevel:
        """Resolve a permission from pre-loaded user data.

        Resolution order: resource match (exact then glob) > tool match > default.
        """
        # Resource-level check
        if resource is not None:
            resource_map: dict[str, str] = data.get("resources", {}).get(tool_name, {})
            if resource in resource_map:
                return PermissionLevel(resource_map[resource])
            for pattern, level in resource_map.items():
                if fnmatch(resource, pattern):
                    return PermissionLevel(level)

        # Tool-level check
        tools: dict[str, str] = data.get("tools", {})
        if tool_name in tools:
            return PermissionLevel(tools[tool_name])

        return default

    async def check_permission(
        self,
        user_id: str,
        tool_name: str,
        resource: str | None = None,
        default: PermissionLevel = PermissionLevel.ASK,
    ) -> PermissionLevel:
        """Check the stored permission for a tool (and optional resource).

        Resolution order: resource match (exact then glob) > tool match > default.
        """
        data = await self._load(user_id)
        return self.resolve_permission(data, tool_name, resource, default)

    def generate_defaults(self, user_id: str) -> dict[str, Any]:
        """Build a complete permissions dict with all tools at their default levels."""
        from backend.app.agent.tools.registry import (
            default_registry,
            ensure_tool_modules_imported,
        )

        ensure_tool_modules_imported()
        tools: dict[str, str] = {}
        for factory_name in sorted(default_registry.factory_names):
            for st in default_registry.get_factory_sub_tools(factory_name):
                tools[st.name] = st.default_permission
        return {"version": _PERMISSIONS_VERSION, "tools": tools, "resources": {}}

    async def ensure_complete(self, user_id: str) -> dict[str, Any]:
        """Load permissions, backfilling any missing tools with defaults."""
        data = await self._load(user_id)
        defaults = self.generate_defaults(user_id)
        changed = False
        for tool_name, default_level in defaults["tools"].items():
            if tool_name not in data.get("tools", {}):
                data.setdefault("tools", {})[tool_name] = default_level
                changed = True
        if changed:
            await self._save(user_id, data)
        return data

    async def reset_permissions(self, user_id: str) -> None:
        """Reset all permissions to defaults."""
        await self._save(user_id, self.generate_defaults(user_id))

    async def get_never_tool_names(self, user_id: str) -> set[str]:
        """Return the set of tool names whose stored permission is ``NEVER``.

        Router and heartbeat code pass this to the registry's
        ``excluded_tool_names`` so a ``NEVER`` sub-tool is never surfaced
        in the LLM schema. Resource-level overrides do not gate schema
        visibility (the tool may still be valid for other resources), so
        they are not considered here.
        """
        data = await self._load(user_id)
        tools = data.get("tools", {})
        if not isinstance(tools, dict):
            return set()
        return {
            name
            for name, level in tools.items()
            if isinstance(level, str) and level == PermissionLevel.NEVER.value
        }

    async def set_permission(
        self,
        user_id: str,
        tool_name: str,
        level: PermissionLevel,
        resource: str | None = None,
    ) -> None:
        """Store a permission override atomically.

        Runs the read-modify-write inside a single transaction guarded by
        a Postgres advisory lock keyed on the user. Otherwise two
        concurrent callers (the approval gate persisting an Always, the
        dashboard PUT, and/or an agent edit_file) could read the same
        snapshot and overwrite each other -- classic lost update. The
        lock plus same-transaction read+write closes the window.

        Backfills the complete tool list before writing so setting one
        permission doesn't drop other entries.

        ``db_session_async`` calls ``await db.commit()`` implicitly via
        its context manager exit only on success; rollback on exception
        drops the lock too. Either way the transaction-scoped lock
        cannot leak past this method.
        """
        async with db_session_async() as db:
            await _lock_user_permissions(db, user_id)
            row = (await db.execute(_select_user_permissions(user_id))).scalar_one_or_none()
            data = _parse_row_data(row)

            # ensure_complete-style backfill: fill missing tool defaults.
            defaults = self.generate_defaults(user_id)
            for tname, default_level in defaults["tools"].items():
                data.setdefault("tools", {}).setdefault(tname, default_level)

            if resource is not None:
                data.setdefault("resources", {}).setdefault(tool_name, {})[resource] = str(level)
            else:
                data.setdefault("tools", {})[tool_name] = str(level)

            payload = json.dumps(data, indent=2, default=str)
            if row is None:
                db.add(UserPermissionSet(user_id=user_id, data=payload))
            else:
                row.data = payload
            await db.commit()


# ---------------------------------------------------------------------------
# ApprovalGate (async coordination)
# ---------------------------------------------------------------------------


@dataclass
class PendingApproval:
    """In-flight approval request waiting for user response."""

    tool_name: str
    description: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: ApprovalDecision | None = None
    # The exact prompt text the user was shown, kept so an ambiguous reply can
    # be answered by re-sending it (see ``note_ambiguous_reply``).
    prompt: str = ""
    # Number of times this approval has been re-prompted after ambiguous replies.
    reprompt_count: int = 0


class ApprovalGate:
    """Manages pending approval requests keyed by user_id.

    When a tool needs approval, ``request_approval()`` sends a prompt and
    waits on an ``asyncio.Event``. When the user replies, ``resolve()``
    sets the decision and wakes the waiting coroutine.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}

    def has_pending(self, user_id: str) -> bool:
        """Return True if there is a pending approval for this user."""
        return user_id in self._pending

    def note_ambiguous_reply(self, user_id: str) -> str | None:
        """Record an ambiguous reply against a pending approval.

        Returns the prompt text to re-send so the caller can ask the user
        for a clear yes/no, leaving the approval pending. Returns ``None``
        when there is no pending approval or the re-prompt cap has been
        reached, in which case the caller should fall back to resolving the
        gate as INTERRUPTED.

        Re-prompting (instead of interrupting) keeps a batched multi-tool
        approval intact when the user replies with filler like "lol" partway
        through: the blocked agent loop stays waiting for a real decision
        rather than aborting the batch and leaving it half-applied.
        """
        pending = self._pending.get(user_id)
        if pending is None or pending.reprompt_count >= _MAX_APPROVAL_REPROMPTS:
            return None
        pending.reprompt_count += 1
        return pending.prompt

    async def request_approval(
        self,
        user_id: str,
        tool_name: str,
        description: str,
        publish_outbound: Callable[[OutboundMessage], Awaitable[None]],
        channel: str,
        chat_id: str,
        timeout: float | None = None,
        prompt: str | None = None,
    ) -> ApprovalDecision:
        """Send an approval prompt and wait for the user's decision.

        When *prompt* is provided it is sent as-is (useful when the caller
        has already formatted a batch plan message).  Otherwise a default
        prompt is built from *tool_name* and *description*.

        Returns ``DENIED`` on timeout.

        The pending request is also persisted to ``pending_approvals`` so
        that if this worker crashes before the user replies, a fresh
        worker can detect the orphan on startup, notify the user, and
        clean up. The in-memory ``_pending`` dict is still the source of
        truth for wake-up signalling within a live worker; the DB row is
        only consulted when a worker boots.
        """
        if timeout is None:
            timeout = float(settings.approval_timeout_seconds)

        pending = PendingApproval(tool_name=tool_name, description=description)
        # Persist the audit row + orphan-detection row BEFORE registering
        # in ``_pending``. ``resolve()`` consults ``_pending`` and bails
        # when the entry is absent, so a concurrent ``resolve`` (e.g. the
        # ``_interrupt_stale_approval`` poll in ingestion that fires
        # ``INTERRUPTED`` when a second message arrives for the same user)
        # cannot run until both writes are committed. Without this
        # ordering, the await on either DB call yields the event loop,
        # the poll task fires, ``resolve`` inserts the ``decided`` row,
        # and the still-pending ``requested`` insert lands afterward,
        # giving an out-of-order audit log (and a possibly orphaned
        # ``pending_approvals`` row when the DELETE in resolve sees no
        # row under READ COMMITTED).
        await _log_approval_event(user_id, "requested", tool_name, description, channel, chat_id)
        await _persist_pending_row(user_id, tool_name, description, channel, chat_id)
        self._pending[user_id] = pending

        if prompt is None:
            prompt = format_approval_message(tool_name, description)
        pending.prompt = prompt
        try:
            await publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=prompt)
            )
        except Exception:
            logger.exception("Failed to send approval prompt to user %s", user_id)
            self._pending.pop(user_id, None)
            await _delete_pending_row(user_id)
            return ApprovalDecision.DENIED

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "Approval timed out after %.0fs for user %s, tool %s. "
                "The user may have responded but the message was not recognized "
                "as an approval response. Resolving as DENIED.",
                timeout,
                user_id,
                tool_name,
            )
            self._pending.pop(user_id, None)
            await _delete_pending_row(user_id)
            await _log_approval_event(
                user_id, "timed_out", tool_name, description, channel, chat_id
            )
            return ApprovalDecision.DENIED

        if pending.decision is None:
            logger.error(
                "Approval event fired without a decision for user %s, tool %s. "
                "Defaulting to DENIED. This indicates resolve() did not set "
                "pending.decision before event.set().",
                user_id,
                tool_name,
            )
            decision = ApprovalDecision.DENIED
        else:
            decision = pending.decision
        self._pending.pop(user_id, None)
        await _delete_pending_row(user_id)
        return decision

    async def resolve(self, user_id: str, decision: ApprovalDecision) -> bool:
        """Resolve a pending approval with the user's decision.

        Returns True if there was a pending approval to resolve.

        The persisted row is deleted before ``event.set()`` fires so that if
        this worker crashes between resolving and the waiting coroutine's
        trailing cleanup, a fresh worker won't mistake the already-answered
        row for an orphan and re-send the recovery message.
        """
        pending = self._pending.get(user_id)
        if pending is None:
            return False
        pending.decision = decision
        await _delete_pending_row(user_id)
        # The audit log mirrors what was sent to the user: tool_name and
        # description come from the in-memory PendingApproval. channel /
        # chat_id are not threaded through resolve() (callers don't know
        # them); they're already on the matching ``requested`` row, so
        # admins can join by user_id + ordering.
        await _log_approval_event(
            user_id,
            "decided",
            pending.tool_name,
            pending.description,
            channel="",
            chat_id="",
            decision=decision,
        )
        pending.event.set()
        return True


# ---------------------------------------------------------------------------
# Persistence for orphan detection
# ---------------------------------------------------------------------------


async def _persist_pending_row(
    user_id: str,
    tool_name: str,
    description: str,
    channel: str,
    chat_id: str,
) -> None:
    """Upsert a pending_approvals row before the prompt is sent.

    Uses a single-statement ON CONFLICT DO UPDATE so two concurrent
    request_approval calls for the same user can't both see "no row"
    and race an INSERT into a PK violation.

    Failures are logged but never raised: persistence is a recovery aid,
    not a correctness prerequisite. The in-memory gate still drives
    the live wake-up flow.
    """
    try:
        now = datetime.now(UTC)
        stmt = (
            pg_insert(PendingApprovalRow)
            .values(
                user_id=user_id,
                tool_name=tool_name,
                description=description,
                channel=channel,
                chat_id=chat_id,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "tool_name": tool_name,
                    "description": description,
                    "channel": channel,
                    "chat_id": chat_id,
                    "created_at": now,
                },
            )
        )
        async with db_session_async() as db:
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.exception(
            "Failed to persist pending approval row for user %s tool %s",
            user_id,
            tool_name,
        )


async def _delete_pending_row(user_id: str) -> None:
    """Delete a pending_approvals row. Logs and swallows errors."""
    try:
        async with db_session_async() as db:
            row = await db.get(PendingApprovalRow, user_id)
            if row is not None:
                await db.delete(row)
                await db.commit()
    except Exception:
        logger.exception("Failed to delete pending approval row for user %s", user_id)


# ---------------------------------------------------------------------------
# Approval audit log
# ---------------------------------------------------------------------------


async def _log_approval_event(
    user_id: str,
    event_type: str,
    tool_name: str,
    description: str,
    channel: str,
    chat_id: str,
    decision: ApprovalDecision | None = None,
) -> None:
    """Append one ``approval_events`` row for an approval-lifecycle transition.

    Failures are logged and swallowed: the audit log is observability,
    not a correctness prerequisite. The in-memory gate still drives the
    live wake-up flow.
    """
    try:
        async with db_session_async() as db:
            db.add(
                ApprovalEvent(
                    user_id=user_id,
                    event_type=event_type,
                    tool_name=tool_name,
                    description=description,
                    channel=channel,
                    chat_id=chat_id,
                    decision=str(decision) if decision is not None else None,
                )
            )
            await db.commit()
    except Exception:
        logger.exception(
            "Failed to record approval_event(%s) for user %s tool %s",
            event_type,
            user_id,
            tool_name,
        )


_ORPHAN_MAX_AGE = timedelta(hours=1)

# Advisory-lock key used to serialize orphan cleanup across workers at boot.
_CLEANUP_LOCK_KEY = "pending_approvals:cleanup"


async def cleanup_orphaned_approvals(
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]],
) -> int:
    """Send a recovery message for every orphaned pending_approvals row.

    Called once on worker startup. Each row represents an approval request
    that was waiting when the previous worker died. The originating agent
    coroutine is gone and can't be resumed, so the best we can do is tell
    the user their prior request was interrupted and clear the row. Returns
    the number of orphans for which a recovery message was successfully
    delivered.

    When several workers boot at once (rolling restart, blue/green, etc.),
    ``pg_try_advisory_lock`` ensures exactly one worker owns the cleanup
    pass. The rest return immediately with zero work done so users don't
    receive duplicate "previous request was interrupted" messages.

    Rows are always deleted by the end of this call:
      * Malformed rows (missing channel or chat_id) are deleted with a
        warning; no message is sent because we have nowhere to send it.
      * Rows older than ``_ORPHAN_MAX_AGE`` are deleted whether or not
        publish succeeds, so a permanently-broken channel can't keep the
        row (and its retry attempts) around forever.
      * Fresh rows whose publish fails stay in the table for a later
        restart to retry. The age gate caps how long that can loop.
    """
    db = AsyncSessionLocal()
    lock_acquired = False
    try:
        try:
            got_lock = (
                await db.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                    {"k": _CLEANUP_LOCK_KEY},
                )
            ).scalar()
            # Close the implicit read transaction so we aren't idle-in-
            # transaction through publish_outbound below. The advisory
            # lock is session-scoped and survives the commit.
            await db.commit()
        except Exception:
            logger.exception("Failed to acquire orphan-cleanup advisory lock")
            return 0
        if not got_lock:
            logger.info("Another worker is running orphan cleanup; skipping on this boot")
            return 0
        lock_acquired = True

        try:
            rows = (await db.execute(select(PendingApprovalRow))).scalars().all()
            snapshot = [(r.user_id, r.tool_name, r.channel, r.chat_id, r.created_at) for r in rows]
            await db.commit()
        except Exception:
            logger.exception("Failed to load orphaned approvals on startup")
            return 0

        now = datetime.now(UTC)
        recovered = 0
        for user_id, tool_name, channel, chat_id, created_at in snapshot:
            if not channel or not chat_id:
                logger.warning(
                    "Dropping malformed pending_approvals row for user %s tool %s: "
                    "channel=%r chat_id=%r",
                    user_id,
                    tool_name,
                    channel,
                    chat_id,
                )
                await _delete_pending_row(user_id)
                continue

            expired = created_at is not None and (now - created_at) > _ORPHAN_MAX_AGE

            try:
                await publish_outbound(
                    OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=(
                            "My previous approval request was interrupted and I didn't "
                            "get your reply. Please resend your last message if you still "
                            "want me to act on it."
                        ),
                    )
                )
                await _delete_pending_row(user_id)
                await _log_approval_event(user_id, "recovered", tool_name, "", channel, chat_id)
                recovered += 1
                logger.info(
                    "Recovered orphaned approval for user %s (tool=%s)",
                    user_id,
                    tool_name,
                )
            except Exception:
                logger.exception(
                    "Failed to recover orphaned approval for user %s tool %s",
                    user_id,
                    tool_name,
                )
                if expired:
                    logger.warning(
                        "Dropping expired pending_approvals row for user %s tool %s "
                        "(age > %s); publish kept failing",
                        user_id,
                        tool_name,
                        _ORPHAN_MAX_AGE,
                    )
                    await _delete_pending_row(user_id)
        return recovered
    finally:
        if lock_acquired:
            try:
                await db.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": _CLEANUP_LOCK_KEY},
                )
                await db.commit()
            except Exception:
                logger.exception("Failed to release orphan-cleanup advisory lock")
        await db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_APPROVAL_RESPONSE_FAST_PATH: dict[str, ApprovalDecision] = {
    # Single-word forms (with the y/n shortcuts).
    "yes": ApprovalDecision.APPROVED,
    "y": ApprovalDecision.APPROVED,
    "always": ApprovalDecision.ALWAYS_ALLOW,
    "no": ApprovalDecision.DENIED,
    "n": ApprovalDecision.DENIED,
    "never": ApprovalDecision.ALWAYS_DENY,
    # Compound forms (always/never anchored with an explicit allow/deny
    # word). Mixed-axis pairs like ``yes never`` are intentionally
    # absent so they fall through to the LLM classifier rather than
    # being silently misclassified.
    "yes always": ApprovalDecision.ALWAYS_ALLOW,
    "always yes": ApprovalDecision.ALWAYS_ALLOW,
    "y always": ApprovalDecision.ALWAYS_ALLOW,
    "always y": ApprovalDecision.ALWAYS_ALLOW,
    "always allow": ApprovalDecision.ALWAYS_ALLOW,
    "allow always": ApprovalDecision.ALWAYS_ALLOW,
    # Blanket tool-level allow ("always all"). Only offered for tools that
    # scope by a resource (e.g. invoice recipients). Stored as a tool-level
    # ALWAYS so every resource is covered, not just the one in front of the
    # user. Conservative phrasings only; anything else falls to the LLM.
    "always all": ApprovalDecision.ALWAYS_ALLOW_ALL,
    "all always": ApprovalDecision.ALWAYS_ALLOW_ALL,
    "allow all": ApprovalDecision.ALWAYS_ALLOW_ALL,
    "always everyone": ApprovalDecision.ALWAYS_ALLOW_ALL,
    "always anyone": ApprovalDecision.ALWAYS_ALLOW_ALL,
    "no never": ApprovalDecision.ALWAYS_DENY,
    "never no": ApprovalDecision.ALWAYS_DENY,
    "n never": ApprovalDecision.ALWAYS_DENY,
    "never n": ApprovalDecision.ALWAYS_DENY,
    "never allow": ApprovalDecision.ALWAYS_DENY,
    "deny always": ApprovalDecision.ALWAYS_DENY,
}


# Punctuation we strip before fast-path lookup. Users naturally type
# ``"Yes."`` / ``"yes, always"`` / ``"yes!"`` and the parser should treat
# those identically to the bare keyword.
_APPROVAL_RESPONSE_PUNCTUATION = ".,!?;:"


def _parse_approval_response(text: str) -> ApprovalDecision | None:
    """Parse a user's text reply into an approval decision (fast path).

    Handles single-word matches and the natural compound replies users
    actually type when they want to lock in a permanent choice
    ("yes always", "always allow", "no never", "never allow", etc.).
    Punctuation (``.,!?;:``) is stripped before lookup so ``"Yes."`` and
    ``"yes, always"`` route through the fast path instead of the LLM
    classifier. Anything still ambiguous after normalization falls
    through to ``classify_approval_response``.

    The compound forms are deliberately conservative: only word pairs
    where both halves agree on the same axis (allow vs deny) match.
    Mixed pairs like "yes never" go to the LLM so we do not silently
    pick the wrong intent.
    """
    cleaned = text.strip().lower()
    cleaned = cleaned.translate(str.maketrans("", "", _APPROVAL_RESPONSE_PUNCTUATION))
    normalized = " ".join(cleaned.split())
    return _APPROVAL_RESPONSE_FAST_PATH.get(normalized)


async def classify_approval_response(
    text: str,
) -> ApprovalDecision | Literal["ambiguous"] | None:
    """Classify a natural-language approval response using an LLM.

    Called when ``_parse_approval_response()`` returns None but an approval
    gate is pending. Uses structured output to resolve ambiguous responses
    like "Yes to both", "go ahead", "sure thing", etc.

    Returns ``AMBIGUOUS_APPROVAL_REPLY`` for short filler ("lol", "huh", "ok
    wait") that is neither a clear yes/no nor a clear new request: the caller
    re-prompts instead of interrupting the pending batch. Returns ``None`` if
    the LLM call fails or the message is a clear, unrelated new request.
    """

    class ApprovalClassification(BaseModel):
        """Structured classification of a user's approval response."""

        decision: Literal[
            "approved",
            "denied",
            "always_allow",
            "always_allow_all",
            "always_deny",
            "ambiguous",
            "unrelated",
        ] = Field(
            description=(
                "Classify the user's message: "
                "'approved' if they are saying yes/agreeing, "
                "'denied' if they are saying no/refusing, "
                "'always_allow' if they want to always allow this specific target "
                "(e.g. 'always', 'always yes'), "
                "'always_allow_all' if they want to always allow this action for every "
                "target, not just this one (e.g. 'always all', 'allow all', 'always everyone'), "
                "'always_deny' if they want to always deny (e.g. 'never', 'never allow'), "
                "'ambiguous' if the message is short filler or unclear (e.g. 'lol', 'haha', "
                "'hmm', 'wait', 'huh') that is neither a clear yes/no nor a clear new request, "
                "'unrelated' if the message is a clear new request or a clear change of subject"
            )
        )

    model = settings.compaction_model or settings.llm_model
    provider = settings.compaction_provider or settings.llm_provider

    try:
        response = cast(
            ChatCompletion,
            await acompletion(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "The user was asked to approve or deny a tool action. "
                            "They were shown a menu: "
                            "yes (allow this once), no (deny this once), "
                            "always (allow and remember this target), "
                            "never (deny and remember). For actions that target a "
                            "specific resource (such as an email recipient) they may "
                            "also have seen 'always all' (allow and remember for every "
                            "target, not just this one). "
                            "Classify their response into one of: approved, denied, "
                            "always_allow, always_allow_all, always_deny, ambiguous, "
                            "unrelated. "
                            "Use 'ambiguous' for short filler or unclear replies that do "
                            "not commit to yes or no and are not a new request; the user "
                            "will be asked to clarify."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                response_format=ApprovalClassification,
                max_tokens=50,
                # No ``temperature``: claude-opus-4-7 (and newer Anthropic
                # models) return 400 ``temperature is deprecated for this
                # model``, which made every fuzzy approval response fall
                # through to the INTERRUPTED fallback in prod. The
                # six-value enum via ``response_format`` already constrains the
                # output; temperature has no meaningful effect on it.
            ),
        )
    except Exception:
        logger.warning("LLM approval classification failed for text: %r", text[:100], exc_info=True)
        return None

    parsed = response.choices[0].message.parsed  # type: ignore[union-attr]
    if parsed is None:
        logger.warning("LLM approval classification returned no parsed result")
        return None

    decision_map: dict[str, ApprovalDecision] = {
        "approved": ApprovalDecision.APPROVED,
        "denied": ApprovalDecision.DENIED,
        "always_allow": ApprovalDecision.ALWAYS_ALLOW,
        "always_allow_all": ApprovalDecision.ALWAYS_ALLOW_ALL,
        "always_deny": ApprovalDecision.ALWAYS_DENY,
    }
    result = decision_map.get(parsed.decision)
    if result is not None:
        logger.info("LLM classified approval response %r as %s", text[:100], result)
        return result
    if parsed.decision == "ambiguous":
        logger.info("LLM classified approval response %r as ambiguous (will re-prompt)", text[:100])
        return AMBIGUOUS_APPROVAL_REPLY
    logger.info("LLM classified response %r as unrelated to approval", text[:100])
    return None


def format_approval_message(
    tool_name: str,
    description: str,
    *,
    offer_blanket: bool = False,
    resource_noun: str | None = None,
) -> str:
    """Build a plain-text approval prompt for the user.

    The reply options are listed as a vertical menu so each choice is
    unambiguous. A previous wording, ``"Reply yes or no (always/never to
    remember your choice)"``, was misread by users as a two-axis answer
    ("yes always" or "no never"); the menu form makes it clear that
    exactly one option is the expected response. The parser still accepts
    the compound forms in case a user types one anyway.

    When *offer_blanket* is True (a resource-scoped tool whose policy
    declares a ``resource_noun``), an extra "always all" option is shown
    between "always" and "never". Picking it grants a tool-level approval
    covering every resource, so the user is not re-prompted for each new
    recipient/target. The "always" line stays scoped to the one in front
    of them. ``resource_noun`` (e.g. "recipients") fills the wording; it
    falls back to "of them" when not supplied.

    The "never: deny and remember" line stays last on purpose:
    ``context.py`` identifies stored approval prompts in history by that
    trailing line, so the blanket option must be inserted before it.
    """
    blanket_line = ""
    if offer_blanket:
        noun = resource_noun or "of them"
        blanket_line = f"  always all: allow for all {noun} and remember\n"

    return (
        f"I'd like to: {description}\n\n"
        "Reply with one of:\n"
        "  yes: allow this once\n"
        "  no: deny this once\n"
        "  always: allow and remember\n"
        f"{blanket_line}"
        "  never: deny and remember"
    )


# ---------------------------------------------------------------------------
# ApprovalEventStore (read-side audit-log access)
# ---------------------------------------------------------------------------


@dataclass
class ApprovalEventRecord:
    """Read-side projection of one ``approval_events`` row."""

    id: int
    user_id: str
    event_type: str
    tool_name: str
    description: str
    channel: str
    chat_id: str
    decision: str | None
    created_at: datetime


class ApprovalEventStore:
    """Read-side accessor for ``approval_events``.

    Premium admin endpoints import this rather than reaching into the
    table directly so the underlying schema can evolve without churn in
    the premium repo.
    """

    async def list_for_user(
        self,
        user_id: str,
        limit: int = 500,
        since: datetime | None = None,
    ) -> list[ApprovalEventRecord]:
        """Return approval events for *user_id* in chronological order.

        ``limit`` caps the query so a long-running admin dashboard
        cannot OOM the response. ``since`` is an inclusive lower bound
        on ``created_at``; pass it to scope to a recent window.
        """
        async with db_session_async() as db:
            stmt = select(ApprovalEvent).where(ApprovalEvent.user_id == user_id)
            if since is not None:
                stmt = stmt.where(ApprovalEvent.created_at >= since)
            rows = (
                (
                    await db.execute(
                        stmt.order_by(ApprovalEvent.created_at.asc(), ApprovalEvent.id.asc()).limit(
                            limit
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [
                ApprovalEventRecord(
                    id=r.id,
                    user_id=r.user_id,
                    event_type=r.event_type,
                    tool_name=r.tool_name,
                    description=r.description or "",
                    channel=r.channel or "",
                    chat_id=r.chat_id or "",
                    decision=r.decision,
                    created_at=r.created_at,
                )
                for r in rows
            ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_approval_gate: ApprovalGate | None = None
_approval_store: ApprovalStore | None = None
_approval_event_store: ApprovalEventStore | None = None


def get_approval_gate() -> ApprovalGate:
    """Get or create the global ApprovalGate."""
    global _approval_gate
    if _approval_gate is None:
        _approval_gate = ApprovalGate()
    return _approval_gate


def get_approval_store() -> ApprovalStore:
    """Get or create the global ApprovalStore."""
    global _approval_store
    if _approval_store is None:
        _approval_store = ApprovalStore()
    return _approval_store


def get_approval_event_store() -> ApprovalEventStore:
    """Get or create the global ApprovalEventStore."""
    global _approval_event_store
    if _approval_event_store is None:
        _approval_event_store = ApprovalEventStore()
    return _approval_event_store


def reset_approval_gate() -> None:
    """Reset cached approval singletons. Used by tests."""
    global _approval_gate, _approval_store, _approval_event_store
    _approval_gate = None
    _approval_store = None
    _approval_event_store = None
