"""Tests for the in-process DEK cache."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from backend.app.security.dek_cache import DEKCache


def test_get_returns_none_for_unknown_key() -> None:
    cache = DEKCache()
    assert cache.get(b"unknown") is None


def test_put_then_get_returns_value() -> None:
    cache = DEKCache()
    cache.put(b"wrapped-1", b"dek-1")
    assert cache.get(b"wrapped-1") == b"dek-1"


def test_get_evicts_expired_entry() -> None:
    cache = DEKCache(ttl_seconds=10)
    fake_now = [1000.0]

    def _now() -> float:
        return fake_now[0]

    with patch("backend.app.security.dek_cache.time.monotonic", side_effect=_now):
        cache.put(b"k", b"v")
        assert cache.get(b"k") == b"v"
        fake_now[0] += 11
        assert cache.get(b"k") is None
        assert cache.size() == 0


def test_put_evicts_oldest_when_at_capacity() -> None:
    cache = DEKCache(ttl_seconds=300, max_entries=2)
    fake_now = [1000.0]

    def _now() -> float:
        return fake_now[0]

    with patch("backend.app.security.dek_cache.time.monotonic", side_effect=_now):
        cache.put(b"a", b"1")
        fake_now[0] += 1
        cache.put(b"b", b"2")
        fake_now[0] += 1
        cache.put(b"c", b"3")  # Evicts "a" (soonest expiring).
        assert cache.get(b"a") is None
        assert cache.get(b"b") == b"2"
        assert cache.get(b"c") == b"3"


def test_put_with_existing_key_does_not_evict_others() -> None:
    """Re-putting an existing key replaces its value; doesn't push others out."""
    cache = DEKCache(ttl_seconds=300, max_entries=2)
    cache.put(b"a", b"1")
    cache.put(b"b", b"2")
    cache.put(b"a", b"1-new")
    assert cache.get(b"a") == b"1-new"
    assert cache.get(b"b") == b"2"


def test_constructor_rejects_invalid_args() -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        DEKCache(ttl_seconds=0)
    with pytest.raises(ValueError, match="max_entries must be positive"):
        DEKCache(max_entries=0)


def test_clear_empties_cache() -> None:
    cache = DEKCache()
    cache.put(b"k", b"v")
    assert cache.size() == 1
    cache.clear()
    assert cache.size() == 0
    assert cache.get(b"k") is None


def test_real_clock_short_ttl() -> None:
    """Smoke test that monotonic-clock TTL actually expires (no mocking)."""
    cache = DEKCache(ttl_seconds=1)
    cache.put(b"k", b"v")
    assert cache.get(b"k") == b"v"
    # Sleep past the TTL with margin for slow CI runners; 1.1s was tight.
    time.sleep(2)
    assert cache.get(b"k") is None
