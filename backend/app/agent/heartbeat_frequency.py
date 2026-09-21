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
