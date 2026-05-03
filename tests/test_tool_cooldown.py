"""Tests for the per-(user, tool, args) cooldown tracker.

Covers the unit behavior of ``ToolCooldownTracker`` (hashing, eviction,
transient-vs-permanent error filtering). The end-to-end short-circuit
path through ``_execute_single_tool`` is exercised in
``test_agent_loop.py``.
"""

from __future__ import annotations

import time

import pytest

from backend.app.agent.tool_cooldown import (
    DEFAULT_COOLDOWN_SECONDS,
    CooldownHit,
    ToolCooldownTracker,
    _hash_args,
    get_tool_cooldown_tracker,
)
from backend.app.agent.tools.base import ToolErrorKind


@pytest.fixture()
def tracker() -> ToolCooldownTracker:
    """Fresh tracker per test, unused otherwise the module singleton
    leaks state across tests."""
    return ToolCooldownTracker(cooldown_seconds=5.0)


# ---------------------------------------------------------------------------
# Hash stability
# ---------------------------------------------------------------------------


def test_hash_args_is_dict_order_invariant() -> None:
    """JSON canonicalisation must absorb dict insertion order, otherwise
    two structurally identical calls hash differently and the cooldown
    fails to fire."""
    a = _hash_args({"a": 1, "b": 2, "c": 3})
    b = _hash_args({"c": 3, "a": 1, "b": 2})
    assert a == b


def test_hash_args_distinguishes_value_changes() -> None:
    a = _hash_args({"recipient": "alice@example.com"})
    b = _hash_args({"recipient": "bob@example.com"})
    assert a != b


def test_hash_args_handles_non_jsonable_values() -> None:
    """Tools should only emit JSON-shaped args, but if a corner case
    smuggles in something exotic the helper must not raise."""
    h = _hash_args({"x": object()})
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# Cooldown semantics
# ---------------------------------------------------------------------------


def test_no_cooldown_for_fresh_call(tracker: ToolCooldownTracker) -> None:
    assert tracker.is_cooling_down("u1", "qb_send", {"id": "543"}) is None


def test_records_service_failure_and_blocks_retry(tracker: ToolCooldownTracker) -> None:
    """SERVICE errors (e.g. QB 500) trigger the cooldown."""
    args = {"entity_type": "Estimate", "entity_id": "543", "email": "x@example.com"}
    tracker.record_failure("u1", "qb_send", args, ToolErrorKind.SERVICE)

    hit = tracker.is_cooling_down("u1", "qb_send", args)
    assert isinstance(hit, CooldownHit)
    assert hit.last_error_kind == ToolErrorKind.SERVICE
    assert hit.seconds_remaining > 0


def test_records_internal_failure_and_blocks_retry(tracker: ToolCooldownTracker) -> None:
    """Unhandled exceptions get classified as INTERNAL and also cool down."""
    args = {"path": "/etc/passwd"}
    tracker.record_failure("u1", "read_file", args, ToolErrorKind.INTERNAL)
    assert tracker.is_cooling_down("u1", "read_file", args) is not None


def test_does_not_cooldown_validation_error(tracker: ToolCooldownTracker) -> None:
    """Validation errors mean the LLM sent bad args. A retry with the
    same args won't succeed but a retry with corrected args should not
    be blocked. So we deliberately do not cool down VALIDATION."""
    args = {"entity_type": "Estimate"}  # missing data field
    tracker.record_failure("u1", "qb_create", args, ToolErrorKind.VALIDATION)
    assert tracker.is_cooling_down("u1", "qb_create", args) is None


def test_does_not_cooldown_permission_or_auth(tracker: ToolCooldownTracker) -> None:
    args = {"x": 1}
    tracker.record_failure("u1", "qb_create", args, ToolErrorKind.PERMISSION)
    tracker.record_failure("u1", "calendar_create_event", args, ToolErrorKind.AUTH)
    assert tracker.is_cooling_down("u1", "qb_create", args) is None
    assert tracker.is_cooling_down("u1", "calendar_create_event", args) is None


def test_does_not_cooldown_when_error_kind_missing(tracker: ToolCooldownTracker) -> None:
    """Tools sometimes return is_error=True without setting error_kind.
    Treat absence as "don't cool down" — be conservative."""
    tracker.record_failure("u1", "qb_send", {"id": "1"}, None)
    assert tracker.is_cooling_down("u1", "qb_send", {"id": "1"}) is None


def test_per_user_isolation(tracker: ToolCooldownTracker) -> None:
    """One user's failure must not block another user's identical call."""
    args = {"id": "543"}
    tracker.record_failure("alice", "qb_send", args, ToolErrorKind.SERVICE)
    assert tracker.is_cooling_down("alice", "qb_send", args) is not None
    assert tracker.is_cooling_down("bob", "qb_send", args) is None


def test_per_args_isolation(tracker: ToolCooldownTracker) -> None:
    """Different args = different cooldown key. Sending estimate 543 is
    independent of sending estimate 544 even when both are flaky."""
    tracker.record_failure("u1", "qb_send", {"id": "543"}, ToolErrorKind.SERVICE)
    assert tracker.is_cooling_down("u1", "qb_send", {"id": "544"}) is None


def test_per_tool_isolation(tracker: ToolCooldownTracker) -> None:
    """A failed qb_send does not block qb_query with the same args."""
    args = {"x": 1}
    tracker.record_failure("u1", "qb_send", args, ToolErrorKind.SERVICE)
    assert tracker.is_cooling_down("u1", "qb_query", args) is None


def test_cooldown_expires_after_window() -> None:
    """Use a microsecond cooldown to keep the test fast."""
    tracker = ToolCooldownTracker(cooldown_seconds=0.05)
    args = {"id": "1"}
    tracker.record_failure("u1", "qb_send", args, ToolErrorKind.SERVICE)
    assert tracker.is_cooling_down("u1", "qb_send", args) is not None
    time.sleep(0.1)
    assert tracker.is_cooling_down("u1", "qb_send", args) is None


def test_reset_clears_all_entries(tracker: ToolCooldownTracker) -> None:
    tracker.record_failure("u1", "qb_send", {"id": "1"}, ToolErrorKind.SERVICE)
    tracker.reset()
    assert tracker.is_cooling_down("u1", "qb_send", {"id": "1"}) is None


def test_module_singleton_returns_same_tracker() -> None:
    a = get_tool_cooldown_tracker()
    b = get_tool_cooldown_tracker()
    assert a is b


def test_default_cooldown_is_30_seconds() -> None:
    """Long enough to absorb three back-to-back 'send it' messages,
    short enough that a real follow-up after the downstream recovers
    is unaffected."""
    assert DEFAULT_COOLDOWN_SECONDS == 30.0
