"""Per-message timestamp markers in rebuilt conversation history.

The LLM never sees the stored ``StoredMessage.timestamp`` directly; instead
``_stored_messages_to_agent_messages`` prepends an absolute, localized
timestamp marker to a message when it is the first in the slice, follows a
>30min gap, or crosses a local-day boundary. These tests pin that behavior
without touching the database (both helpers are pure functions).
"""

from backend.app.agent.context import (
    _stored_messages_to_agent_messages,
    _time_marker,
)
from backend.app.agent.file_store import StoredMessage
from backend.app.agent.messages import AssistantMessage, UserMessage

# A fixed Monday afternoon anchor so weekday/AM-PM rendering is deterministic.
_BASE = "2026-06-01T13:00:00+00:00"  # 13:00 UTC == 09:00 America/New_York (EDT)


def _iso(hours: float = 0.0, *, base: str = _BASE) -> str:
    import datetime

    dt = datetime.datetime.fromisoformat(base) + datetime.timedelta(hours=hours)
    return dt.isoformat()


# --- _time_marker -----------------------------------------------------------


def test_first_message_always_anchored() -> None:
    """Empty ``prev_iso`` (first visible message) always yields a marker."""
    marker = _time_marker("", _BASE, "")
    assert marker is not None
    assert "2026-06-01" in marker
    assert "01:00 PM" in marker


def test_small_gap_same_day_no_marker() -> None:
    assert _time_marker(_BASE, _iso(hours=0.25), "") is None  # +15 min


def test_large_gap_marks() -> None:
    assert _time_marker(_BASE, _iso(hours=1), "") is not None  # +60 min


def test_exactly_threshold_marks() -> None:
    """The threshold is exclusive: a gap of exactly 30 min is still marked."""
    assert _time_marker(_BASE, _iso(hours=0.5), "") is not None


def test_day_boundary_marks_despite_small_gap() -> None:
    """A 20-minute gap that crosses local midnight still gets a marker."""
    late = "2026-06-01T23:50:00+00:00"
    early = "2026-06-02T00:10:00+00:00"
    assert _time_marker(late, early, "") is not None


def test_timezone_localizes_marker() -> None:
    """The marker renders in the user's timezone, not UTC."""
    utc = _time_marker("", _BASE, "")
    ny = _time_marker("", _BASE, "America/New_York")
    assert utc is not None and ny is not None
    assert "01:00 PM" in utc
    assert "09:00 AM" in ny


def test_malformed_current_timestamp_returns_none() -> None:
    """A row with an unparseable timestamp is left unmarked, never crashes."""
    assert _time_marker("", "not-a-timestamp", "") is None


def test_malformed_previous_timestamp_treated_as_anchor() -> None:
    """An unparseable ``prev_iso`` falls back to anchoring the current row."""
    assert _time_marker("garbage", _BASE, "") is not None


# --- _stored_messages_to_agent_messages integration -------------------------


def test_marker_prepended_to_first_message() -> None:
    msgs = [
        StoredMessage(direction="inbound", body="Hello", seq=1, timestamp=_BASE),
        StoredMessage(direction="inbound", body="Current", seq=2, timestamp=_iso(hours=0.1)),
    ]
    history = _stored_messages_to_agent_messages(msgs)
    first, second = history[0], history[1]
    assert isinstance(first, UserMessage)
    assert isinstance(second, UserMessage)
    assert first.content.startswith("[")
    assert first.content.endswith("Hello")
    # Second message is within the gap threshold: unmarked, exact body.
    assert second.content == "Current"


def test_outbound_prose_carries_marker_after_gap() -> None:
    """An outbound reply after a >30min gap is marked on its prose."""
    msgs = [
        StoredMessage(direction="inbound", body="Hi", seq=1, timestamp=_BASE),
        StoredMessage(direction="outbound", body="Later reply", seq=2, timestamp=_iso(hours=2)),
    ]
    history = _stored_messages_to_agent_messages(msgs)
    reply = history[1]
    assert isinstance(reply, AssistantMessage)
    assert reply.content is not None
    assert reply.content.startswith("[")
    assert reply.content.endswith("Later reply")


def test_dropped_blank_row_does_not_advance_gap_baseline() -> None:
    """Blank placeholder rows are filtered and must not reset the gap clock.

    The gap for the third message is measured from the first *visible*
    message (t+0), so a 40-minute span still trips the threshold even though
    the dropped blank row sat 20 minutes in.
    """
    msgs = [
        StoredMessage(direction="inbound", body="First", seq=1, timestamp=_BASE),
        StoredMessage(direction="inbound", body="", seq=2, timestamp=_iso(hours=0.33)),
        StoredMessage(direction="inbound", body="Resumed", seq=3, timestamp=_iso(hours=0.67)),
    ]
    history = _stored_messages_to_agent_messages(msgs)
    assert len(history) == 2  # blank row dropped
    resumed = history[1]
    assert isinstance(resumed, UserMessage)
    assert resumed.content.startswith("[")
    assert resumed.content.endswith("Resumed")


def test_no_timezone_falls_back_to_utc_render() -> None:
    msgs = [StoredMessage(direction="inbound", body="Hi", seq=1, timestamp=_BASE)]
    history = _stored_messages_to_agent_messages(msgs, tz_name="")
    first = history[0]
    assert isinstance(first, UserMessage)
    assert "01:00 PM" in first.content
