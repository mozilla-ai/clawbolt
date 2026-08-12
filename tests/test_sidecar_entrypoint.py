"""Tests for the sidecar container entrypoint.

The entrypoint is shell, so there is no unit to call. It is read as text and
asserted against instead, in the same spirit as the source-parsing lock test in
``test_sidecar_locks.py``: the failure modes here are silent until a container
event that may be months apart, so an assertion is worth more than a comment.

What is guarded is the Xvfb startup ordering. A Railway restart reuses the
container filesystem, unlike a redeploy, so ``/tmp/.X99-lock`` survives from the
previous run and Xvfb refuses to start on a display it believes is live. That
took the production sidecar down on 2026-08-12: the entrypoint exited FATAL,
Railway exhausted its three retries in about ten seconds, and the deployment sat
CRASHED until someone redeployed it (issue #1499).
"""

import re
from pathlib import Path

import pytest

_ENTRYPOINT = (
    Path(__file__).resolve().parents[1] / "sidecar" / "home_depot" / "docker-entrypoint.sh"
)


def _lines() -> list[str]:
    return _ENTRYPOINT.read_text().splitlines()


def _index_of(pattern: str) -> int:
    """Return the line number matching ``pattern``, failing loudly if absent."""
    matcher = re.compile(pattern)
    for number, line in enumerate(_lines()):
        if matcher.search(line):
            return number
    pytest.fail(f"no line in docker-entrypoint.sh matches {pattern!r}")


class TestStaleDisplayLock:
    def test_the_lock_is_cleared_before_xvfb_starts(self) -> None:
        """Clearing it afterwards would be useless; Xvfb has already refused."""
        assert _index_of(r"^rm -f .*-lock") < _index_of(r"^Xvfb ")

    def test_the_socket_is_cleared_too(self) -> None:
        """A lock with no socket still counts as an active display to Xvfb."""
        lines = _lines()
        removal = lines[_index_of(r"^rm -f .*-lock")]
        assert ".X11-unix/X" in removal

    def test_the_removal_is_forced(self) -> None:
        """`set -e` is on and a fresh container has neither file.

        Without `-f` this fix would trade a broken restart for a broken cold
        start, which is the more common path by far.
        """
        assert _index_of(r"^rm -f .*-lock") == _index_of(r"^rm -f ")

    def test_the_display_number_is_not_hardcoded(self) -> None:
        """HD_DISPLAY is configurable, so removing :99 by hand would miss it."""
        removal = _lines()[_index_of(r"^rm -f .*-lock")]
        assert "$DISPLAY_NUM" in removal or "${DISPLAY_NUM}" in removal
