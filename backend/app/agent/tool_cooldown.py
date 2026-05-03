"""Per-(user, tool, args) cooldown for transient tool failures.

Stops the agent from re-firing an idempotent-looking tool call against a
flaky downstream within seconds of a failure. The agent loop has no
intrinsic backoff: if a user says "send it" three times after the first
QuickBooks ``qb_send`` returns 500, the agent sends three identical
requests in ~10 seconds, hammering the same broken endpoint.

When ``_execute_single_tool`` is about to dispatch a call that recently
failed transiently (``SERVICE`` or ``INTERNAL`` error_kind), we
short-circuit with a clear "I just tried that" message instead. The
synthetic result is recorded as a tool error so the LLM can adapt its
reply, and the user sees deterministic behavior instead of three
identical 500 receipts.

Cooldown is keyed on (user_id, tool_name, args_hash) so a different
arg shape (e.g. ``send`` to a different recipient) is not blocked. The
hash is a SHA-256 of the canonical JSON form of ``validated_args`` so
dict ordering does not skew the key.

State lives in process memory: a worker restart drops the cache, which
is fine because failures recorded across a restart almost certainly
predate a fix. We intentionally avoid persisting this so the cache
cannot become stale across deploys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from backend.app.agent.tools.base import ToolErrorKind

logger = logging.getLogger(__name__)


# Error kinds that warrant a cooldown. Validation/permission/auth/etc.
# are not transient: re-firing won't suddenly succeed and the LLM should
# adjust args or escalate to the user instead.
_TRANSIENT_KINDS: frozenset[ToolErrorKind] = frozenset(
    {ToolErrorKind.SERVICE, ToolErrorKind.INTERNAL}
)

# Default cooldown window. 30s is long enough to absorb back-to-back
# user "send it" messages but short enough that a real "try again later"
# from the user (after the downstream recovers) is unaffected.
DEFAULT_COOLDOWN_SECONDS: float = 30.0


@dataclass(frozen=True)
class CooldownHit:
    """Returned when a call is on cooldown.

    ``seconds_remaining`` is the lower bound on how long the LLM should
    wait before retrying the same args. ``last_error`` is included so the
    synthetic result can echo what the user originally got.
    """

    seconds_remaining: float
    last_error_kind: ToolErrorKind


def _hash_args(args: dict[str, Any]) -> str:
    """Stable hash of validated tool args.

    Uses ``sort_keys=True`` so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}``
    produce the same key. Recurses into nested dicts. Non-JSON-serializable
    values fall through ``default=str`` (rare in practice: tools validate
    args via a Pydantic model and ``model_dump()`` produces JSON-shaped
    dicts). The outer ``except`` is a belt-and-braces fallback in case
    a value rejects ``str()`` itself; it serializes the sorted ``items``
    via ``repr`` so the hash never raises and we degrade to a usable key.
    """
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(sorted(args.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolCooldownTracker:
    """Bounded in-memory cooldown cache.

    Thread-safe via a single ``Lock`` because the agent loop runs tool
    batches concurrently with ``asyncio.gather``: parallel calls in the
    same turn can read/write this cache from different tasks scheduled
    on the same event loop, but the GIL plus the lock keep the dict
    coherent. ``_evict_expired`` is called inline on each ``record_failure``
    so the dict cannot grow without bound.
    """

    def __init__(self, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS) -> None:
        self._cooldown = cooldown_seconds
        # key -> (expires_at_monotonic, error_kind)
        self._entries: dict[tuple[str, str, str], tuple[float, ToolErrorKind]] = {}
        self._lock = Lock()

    def is_cooling_down(
        self, user_id: str, tool_name: str, validated_args: dict[str, Any]
    ) -> CooldownHit | None:
        """Return a CooldownHit if the call is on cooldown, else None."""
        key = (user_id, tool_name, _hash_args(validated_args))
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, kind = entry
            if expires_at <= now:
                # Lazy eviction.
                self._entries.pop(key, None)
                return None
            return CooldownHit(seconds_remaining=expires_at - now, last_error_kind=kind)

    def record_failure(
        self,
        user_id: str,
        tool_name: str,
        validated_args: dict[str, Any],
        error_kind: ToolErrorKind | None,
    ) -> None:
        """Record a transient tool failure. No-op for non-transient kinds."""
        if error_kind not in _TRANSIENT_KINDS:
            return
        key = (user_id, tool_name, _hash_args(validated_args))
        now = time.monotonic()
        with self._lock:
            self._entries[key] = (now + self._cooldown, error_kind)
            # Evict any expired entries opportunistically so the dict
            # stays bounded under sustained call volume. Cheap because
            # the dict is small in normal operation (one entry per
            # active user with a recent transient failure).
            expired = [k for k, (exp, _) in self._entries.items() if exp <= now]
            for k in expired:
                self._entries.pop(k, None)

    def reset(self) -> None:
        """Clear all entries. Used by tests."""
        with self._lock:
            self._entries.clear()


# Module-level singleton. Reset only by tests via the fixture below.
_tracker = ToolCooldownTracker()


def get_tool_cooldown_tracker() -> ToolCooldownTracker:
    """Accessor for the singleton tracker."""
    return _tracker
