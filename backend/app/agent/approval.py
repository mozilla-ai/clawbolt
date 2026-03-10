"""Progressive approval system for tool execution.

Provides a permission layer that lets users control what the agent can do
autonomously vs. what requires explicit approval. Tools opt in by setting
an ``approval_policy`` on their ``Tool`` definition.

Three permission levels: AUTO (execute freely), ASK (prompt user first),
DENY (never execute). Users can respond with yes/always/no/never to
control both immediate and future behavior.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.app.agent.file_store import _read_json, _user_dir, _write_json
from backend.app.config import settings

if TYPE_CHECKING:
    from backend.app.bus import OutboundMessage

logger = logging.getLogger(__name__)

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

    def _permissions_path(self, user_id: int) -> Path:
        return _user_dir(user_id) / "permissions.json"

    def _load(self, user_id: int) -> dict[str, Any]:
        data = _read_json(self._permissions_path(user_id), default=None)
        if data is None or not isinstance(data, dict):
            return {"version": _PERMISSIONS_VERSION, "tools": {}, "resources": {}}
        return data

    def _save(self, user_id: int, data: dict[str, Any]) -> None:
        _write_json(self._permissions_path(user_id), data)

    def check_permission(
        self,
        user_id: int,
        tool_name: str,
        resource: str | None = None,
        default: PermissionLevel = PermissionLevel.ASK,
    ) -> PermissionLevel:
        """Check the stored permission for a tool (and optional resource).

        Resolution order: resource match (exact then glob) > tool match > default.
        """
        data = self._load(user_id)

        # Resource-level check
        if resource is not None:
            resource_map: dict[str, str] = data.get("resources", {}).get(tool_name, {})
            # Exact match first
            if resource in resource_map:
                return PermissionLevel(resource_map[resource])
            # Glob match
            for pattern, level in resource_map.items():
                if fnmatch(resource, pattern):
                    return PermissionLevel(level)

        # Tool-level check
        tools: dict[str, str] = data.get("tools", {})
        if tool_name in tools:
            return PermissionLevel(tools[tool_name])

        return default

    def set_permission(
        self,
        user_id: int,
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
        self._pending: dict[int, PendingApproval] = {}

    def has_pending(self, user_id: int) -> bool:
        """Return True if there is a pending approval for this user."""
        return user_id in self._pending

    async def request_approval(
        self,
        user_id: int,
        tool_name: str,
        description: str,
        publish_outbound: Callable[[OutboundMessage], Awaitable[None]],
        channel: str,
        chat_id: str,
        timeout: float | None = None,
    ) -> ApprovalDecision:
        """Send an approval prompt and wait for the user's decision.

        Returns ``DENIED`` on timeout.
        """
        if timeout is None:
            timeout = float(settings.approval_timeout_seconds)

        pending = PendingApproval(tool_name=tool_name, description=description)
        self._pending[user_id] = pending

        prompt = _format_approval_message(tool_name, description)
        try:
            from backend.app.bus import OutboundMessage as OMsg

            await publish_outbound(OMsg(channel=channel, chat_id=chat_id, content=prompt))
        except Exception:
            logger.exception("Failed to send approval prompt to user %d", user_id)
            self._pending.pop(user_id, None)
            return ApprovalDecision.DENIED

        try:
            await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        except TimeoutError:
            logger.info("Approval timed out for user %d, tool %s", user_id, tool_name)
            self._pending.pop(user_id, None)
            return ApprovalDecision.DENIED

        decision = pending.decision or ApprovalDecision.DENIED
        self._pending.pop(user_id, None)
        return decision

    def resolve(self, user_id: int, decision: ApprovalDecision) -> bool:
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
    """Parse a user's text reply into an approval decision.

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


def _format_approval_message(tool_name: str, description: str) -> str:
    """Build a plain-text approval prompt for the user."""
    return (
        f"The assistant wants to use the tool '{tool_name}':\n"
        f"{description}\n\n"
        "Reply: yes | no | always | never"
    )


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
