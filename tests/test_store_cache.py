"""Tests for backend.app.agent.store_cache.StoreCache."""

from backend.app.agent.store_cache import StoreCache


class _FakeStore:
    """Minimal store for testing."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


def test_get_creates_instance() -> None:
    cache: StoreCache[_FakeStore] = StoreCache(_FakeStore)
    store = cache.get("user-1")
    assert store.user_id == "user-1"


def test_get_returns_cached_instance() -> None:
    cache: StoreCache[_FakeStore] = StoreCache(_FakeStore)
    a = cache.get("user-1")
    b = cache.get("user-1")
    assert a is b


def test_evicts_oldest_when_full() -> None:
    cache: StoreCache[_FakeStore] = StoreCache(_FakeStore, max_size=2)
    cache.get("a")
    cache.get("b")
    cache.get("c")  # evicts "a"
    # "a" was evicted, so a new instance is returned
    new_a = cache.get("a")
    assert new_a.user_id == "a"
    # "b" should have been evicted by now (LRU order: b, c -> c, a -> evict b)
    # Actually after get("c") evicts "a": [b, c]
    # Then get("a") evicts "b": [c, a]
    new_b = cache.get("b")
    assert new_b.user_id == "b"


def test_lru_reorder_on_access() -> None:
    cache: StoreCache[_FakeStore] = StoreCache(_FakeStore, max_size=2)
    first_a = cache.get("a")
    cache.get("b")
    # Access "a" again to make it most-recently-used
    cache.get("a")
    # Insert "c" -- should evict "b" (least recently used), not "a"
    cache.get("c")
    # "a" should still be the original instance
    assert cache.get("a") is first_a


def test_clear_removes_all() -> None:
    cache: StoreCache[_FakeStore] = StoreCache(_FakeStore)
    first = cache.get("user-1")
    cache.clear()
    second = cache.get("user-1")
    assert first is not second
