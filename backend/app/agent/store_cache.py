"""Shared LRU cache for per-user store instances.

SessionStore, MemoryStore, and other per-user stores all need an LRU cache
bounded to a fixed number of entries to prevent unbounded memory growth in
multi-tenant deployments. This module provides a single reusable class
instead of duplicating the OrderedDict logic in every store module.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

_DEFAULT_MAX_SIZE = 256


class StoreCache(Generic[T]):
    """Bounded LRU cache for per-user store instances.

    Usage::

        _cache: StoreCache[SessionStore] = StoreCache(SessionStore)

        def get_session_store(user_id: str) -> SessionStore:
            return _cache.get(user_id)
    """

    def __init__(
        self,
        factory: Callable[[str], T],
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        self._factory = factory
        self._max_size = max_size
        self._entries: OrderedDict[str, T] = OrderedDict()

    def get(self, key: str) -> T:
        """Return the cached store for *key*, creating it if absent."""
        if key in self._entries:
            self._entries.move_to_end(key)
            return self._entries[key]
        store = self._factory(key)
        self._entries[key] = store
        if len(self._entries) > self._max_size:
            self._entries.popitem(last=False)
        return store

    def clear(self) -> None:
        """Remove all entries (used by tests)."""
        self._entries.clear()
