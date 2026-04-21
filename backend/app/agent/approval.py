"""Progressive approval system for tool execution.

Provides a permission layer that lets users control what the agent can do
autonomously vs. what requires explicit approval. Tools opt in by setting
an ``approval_policy`` on their ``Tool`` definition.

Three permission levels: ALWAYS (execute freely), ASK (prompt user first),
DENY (never execute). Users can respond with yes/always/no/never to
control both immediate and future behavior.

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
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.app.bus import OutboundMessage
from backend.app.config import settings
from backend.app.database import SessionLocal, db_session
from backend.app.models import PendingApprovalRow, UserPermissionSet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ApprovalStore helpers
# ---------------------------------------------------------------------------


def _lock_user_permissions(db: Any, user_id: str) -> None:
    """Acquire a transaction-scoped Postgres advisory lock for this user's
    permissions row.

    Serializes concurrent read-modify-write sequences across workers and
    requests. Released automatically at COMMIT / ROLLBACK. Postgres-only;
    the project's test + production databases are both Postgres.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"user_permissions:{user_id}"},
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


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PermissionLevel(StrEnum):
    """Permission level for a tool or resource."""

    ALWAYS = "always"
    ASK = "ask"
    DENY = "deny"


class ApprovalDecision(StrEnum):
    """User's decision when prompted for approval."""

    APPROVED = "approved"
    DENIED = "denied"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_DENY = "always_deny"
    INTERRUPTED = "interrupted"


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
    """

    default_level: PermissionLevel = PermissionLevel.ASK
    resource_extractor: Callable[[dict[str, Any]], str | None] | None = None
    description_builder: Callable[[dict[str, Any]], str] | None = None


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

    _reply_line = "Reply yes or no (always/never to remember your choice)"

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
            "tools": {"web_search": "always", "bash_exec": "deny"},
            "resources": {
                "web_fetch": {"homedepot.com": "always", "*.gov": "always"}
            }
        }

    Resolution order: resource match (exact then glob) > tool match > policy default.
    """

    def _load(self, user_id: str) -> dict[str, Any]:
        with db_session() as db:
            row = db.query(UserPermissionSet).filter_by(user_id=user_id).first()
            return _parse_row_data(row)

    def _save(self, user_id: str, data: dict[str, Any]) -> None:
        """Wholesale replace. Serialized against concurrent writers via an
        advisory lock keyed on the user so the dashboard PUT can't race
        with set_permission or a workspace_tools write on the same row.
        """
        payload = json.dumps(data, indent=2, default=str)
        with db_session() as db:
            _lock_user_permissions(db, user_id)
            row = db.query(UserPermissionSet).filter_by(user_id=user_id).first()
            if row is None:
                db.add(UserPermissionSet(user_id=user_id, data=payload))
            else:
                row.data = payload
            db.commit()

    def load_user_permissions(self, user_id: str) -> dict[str, Any]:
        """Load the raw permission data for a user.

        Use with :meth:`resolve_permission` for bulk lookups to avoid
        repeated file reads.
        """
        return self._load(user_id)

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

    def check_permission(
        self,
        user_id: str,
        tool_name: str,
        resource: str | None = None,
        default: PermissionLevel = PermissionLevel.ASK,
    ) -> PermissionLevel:
        """Check the stored permission for a tool (and optional resource).

        Resolution order: resource match (exact then glob) > tool match > default.
        """
        data = self._load(user_id)
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

    def ensure_complete(self, user_id: str) -> dict[str, Any]:
        """Load permissions, backfilling any missing tools with defaults."""
        data = self._load(user_id)
        defaults = self.generate_defaults(user_id)
        changed = False
        for tool_name, default_level in defaults["tools"].items():
            if tool_name not in data.get("tools", {}):
                data.setdefault("tools", {})[tool_name] = default_level
                changed = True
        if changed:
            self._save(user_id, data)
        return data

    def reset_permissions(self, user_id: str) -> None:
        """Reset all permissions to defaults."""
        self._save(user_id, self.generate_defaults(user_id))

    def set_permission(
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
        """
        with db_session() as db:
            _lock_user_permissions(db, user_id)
            row = db.query(UserPermissionSet).filter_by(user_id=user_id).first()
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
            db.commit()


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
        self._pending[user_id] = pending
        _persist_pending_row(user_id, tool_name, description, channel, chat_id)

        if prompt is None:
            prompt = format_approval_message(tool_name, description)
        try:
            await publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=prompt)
            )
        except Exception:
            logger.exception("Failed to send approval prompt to user %s", user_id)
            self._pending.pop(user_id, None)
            _delete_pending_row(user_id)
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
            _delete_pending_row(user_id)
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
        _delete_pending_row(user_id)
        return decision

    def resolve(self, user_id: str, decision: ApprovalDecision) -> bool:
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
        _delete_pending_row(user_id)
        pending.event.set()
        return True


# ---------------------------------------------------------------------------
# Persistence for orphan detection
# ---------------------------------------------------------------------------


def _persist_pending_row(
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
        with db_session() as db:
            db.execute(stmt)
            db.commit()
    except Exception:
        logger.exception(
            "Failed to persist pending approval row for user %s tool %s",
            user_id,
            tool_name,
        )


def _delete_pending_row(user_id: str) -> None:
    """Delete a pending_approvals row. Logs and swallows errors."""
    try:
        with db_session() as db:
            row = db.get(PendingApprovalRow, user_id)
            if row is not None:
                db.delete(row)
                db.commit()
    except Exception:
        logger.exception("Failed to delete pending approval row for user %s", user_id)


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
    db = SessionLocal()
    lock_acquired = False
    try:
        try:
            got_lock = db.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": _CLEANUP_LOCK_KEY},
            ).scalar()
            # Close the implicit read transaction so we aren't idle-in-
            # transaction through publish_outbound below. The advisory
            # lock is session-scoped and survives the commit.
            db.commit()
        except Exception:
            logger.exception("Failed to acquire orphan-cleanup advisory lock")
            return 0
        if not got_lock:
            logger.info("Another worker is running orphan cleanup; skipping on this boot")
            return 0
        lock_acquired = True

        try:
            rows = db.query(PendingApprovalRow).all()
            snapshot = [(r.user_id, r.tool_name, r.channel, r.chat_id, r.created_at) for r in rows]
            db.commit()
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
                _delete_pending_row(user_id)
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
                _delete_pending_row(user_id)
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
                    _delete_pending_row(user_id)
        return recovered
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": _CLEANUP_LOCK_KEY},
                )
                db.commit()
            except Exception:
                logger.exception("Failed to release orphan-cleanup advisory lock")
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_approval_response(text: str) -> ApprovalDecision | None:
    """Parse a user's text reply into an approval decision (fast path).

    Handles exact single-word matches only. For natural-language responses
    like "Yes to both" or "go ahead", use ``classify_approval_response()``.

    Returns None if the text is not a recognized approval response.
    """
    normalized = text.strip().lower()
    mapping: dict[str, ApprovalDecision] = {
        "yes": ApprovalDecision.APPROVED,
        "y": ApprovalDecision.APPROVED,
        "always": ApprovalDecision.ALWAYS_ALLOW,
        "no": ApprovalDecision.DENIED,
        "n": ApprovalDecision.DENIED,
        "never": ApprovalDecision.ALWAYS_DENY,
    }
    return mapping.get(normalized)


async def classify_approval_response(text: str) -> ApprovalDecision | None:
    """Classify a natural-language approval response using an LLM.

    Called when ``_parse_approval_response()`` returns None but an approval
    gate is pending. Uses structured output to resolve ambiguous responses
    like "Yes to both", "go ahead", "sure thing", etc.

    Returns None if the LLM call fails or the response is not approval-related.
    """

    class ApprovalClassification(BaseModel):
        """Structured classification of a user's approval response."""

        decision: Literal["approved", "denied", "always_allow", "always_deny", "unrelated"] = Field(
            description=(
                "Classify the user's message: "
                "'approved' if they are saying yes/agreeing, "
                "'denied' if they are saying no/refusing, "
                "'always_allow' if they want to always allow (e.g. 'always', 'always yes'), "
                "'always_deny' if they want to always deny (e.g. 'never', 'never allow'), "
                "'unrelated' if the message is not an approval response at all"
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
                            "They were told: "
                            "'Reply yes or no (always/never to remember your choice)'. "
                            "Classify their response."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                response_format=ApprovalClassification,
                max_tokens=50,
                temperature=0,
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
        "always_deny": ApprovalDecision.ALWAYS_DENY,
    }
    result = decision_map.get(parsed.decision)
    if result is not None:
        logger.info("LLM classified approval response %r as %s", text[:100], result)
    else:
        logger.info("LLM classified response %r as unrelated to approval", text[:100])
    return result


def format_approval_message(tool_name: str, description: str) -> str:
    """Build a plain-text approval prompt for the user."""
    return f"I'd like to: {description}\n\nReply yes or no (always/never to remember your choice)"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_approval_gate: ApprovalGate | None = None
_approval_store: ApprovalStore | None = None


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


def reset_approval_gate() -> None:
    """Reset cached approval singletons. Used by tests."""
    global _approval_gate, _approval_store
    _approval_gate = None
    _approval_store = None
