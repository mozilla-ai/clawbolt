"""Progressive approval system for tool execution.

Provides a permission layer that lets users control what the agent can do
autonomously vs. what requires explicit approval. Tools opt in by setting
an ``approval_policy`` on their ``Tool`` definition.

Three permission levels: AUTO (execute freely), ASK (prompt user first),
DENY (never execute). Users can respond with yes/always/no/never to
control both immediate and future behavior.

Batch plan approval: when a user request triggers multiple tools, the
system presents a single plan message grouping auto and pending steps.
The user approves or rejects the entire batch with one response.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from any_llm import acompletion
from any_llm.types.completion import ChatCompletion
from pydantic import BaseModel, Field

from backend.app.config import settings

if TYPE_CHECKING:
    from backend.app.bus import OutboundMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File I/O helpers (self-contained, no file_store dependency)
# ---------------------------------------------------------------------------


def _user_dir(user_id: str) -> Path:
    """Return the directory for a specific user."""
    return Path(settings.data_dir) / str(user_id)


def _read_json(path: Path, default: Any = None) -> Any:
    """Read and parse a JSON file. Returns default if missing/corrupt."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    """Write data as JSON to a file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PermissionLevel(StrEnum):
    """Permission level for a tool or resource."""

    AUTO = "auto"
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
            "tools": {"web_search": "auto", "bash_exec": "deny"},
            "resources": {
                "web_fetch": {"homedepot.com": "auto", "*.gov": "auto"}
            }
        }

    Resolution order: resource match (exact then glob) > tool match > policy default.
    """

    def _permissions_path(self, user_id: str) -> Path:
        return _user_dir(user_id) / "permissions.json"

    def _load(self, user_id: str) -> dict[str, Any]:
        data = _read_json(self._permissions_path(user_id), default=None)
        if data is None or not isinstance(data, dict):
            return {"version": _PERMISSIONS_VERSION, "tools": {}, "resources": {}}
        return data

    def _save(self, user_id: str, data: dict[str, Any]) -> None:
        _write_json(self._permissions_path(user_id), data)

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

    def set_permission(
        self,
        user_id: str,
        tool_name: str,
        level: PermissionLevel,
        resource: str | None = None,
    ) -> None:
        """Store a permission override for a tool or resource."""
        data = self._load(user_id)
        if resource is not None:
            resources = data.setdefault("resources", {})
            tool_resources = resources.setdefault(tool_name, {})
            tool_resources[resource] = str(level)
        else:
            data.setdefault("tools", {})[tool_name] = str(level)
        self._save(user_id, data)


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
        """
        if timeout is None:
            timeout = float(settings.approval_timeout_seconds)

        pending = PendingApproval(tool_name=tool_name, description=description)
        self._pending[user_id] = pending

        if prompt is None:
            prompt = _format_approval_message(tool_name, description)
        try:
            from backend.app.bus import OutboundMessage as OMsg

            await publish_outbound(OMsg(channel=channel, chat_id=chat_id, content=prompt))
        except Exception:
            logger.exception("Failed to send approval prompt to user %s", user_id)
            self._pending.pop(user_id, None)
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
            return ApprovalDecision.DENIED

        decision = pending.decision or ApprovalDecision.DENIED
        self._pending.pop(user_id, None)
        return decision

    def resolve(self, user_id: str, decision: ApprovalDecision) -> bool:
        """Resolve a pending approval with the user's decision.

        Returns True if there was a pending approval to resolve.
        """
        pending = self._pending.get(user_id)
        if pending is None:
            return False
        pending.decision = decision
        pending.event.set()
        return True


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


def _format_approval_message(tool_name: str, description: str) -> str:
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
