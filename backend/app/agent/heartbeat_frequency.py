"""Heartbeat frequency strings to minutes.

The user sets a cadence in plain words ("6h", "daily", "weekly"); the
scheduler needs minutes. Isolated from the engine so the parsing rules can
be read and tested on their own.
"""

from __future__ import annotations

import re

_FREQ_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)

_NAMED_FREQUENCIES: dict[str, int] = {
    "daily": 1440,
    "weekdays": 1440,
    "weekly": 10080,
}

# How far back to look when building heartbeat history context for the LLM.
_HISTORY_LOOKBACK_DAYS = 7

# Literal prefix that identifies the heartbeat-driven (scheduled) message
# path to the agent. Phase 2's ``task_context`` starts with this string.
# ``backend/app/agent/tools/heartbeat_tools.py`` matches on the same
# constant in ``update_heartbeat``'s ``usage_hint`` to scope proactive
# pruning to the heartbeat path; without that gate, the user-driven
# agent could start removing HEARTBEAT.md lines mid-conversation. Both
# sides import this constant so the strings cannot drift.
SCHEDULED_TASK_PREFIX = "Execute this scheduled task now"


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
