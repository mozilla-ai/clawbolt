"""Bounded in-memory TTL cache for web search responses."""

import asyncio
from typing import Any

from cachetools import TTLCache


class SearchCache:
    """Thread-safe TTL cache for web search results.

    Keyed by (provider, normalized_query, max_results). The TTL is short by
    design: search results are the freshest thing this integration has, and a
    long TTL would trade the one property that makes them worth paying for.
    It exists to absorb the retry-and-reword loop a model runs within a single
    conversation, not to serve tomorrow's prices from today's cache.
    """

    def __init__(self, maxsize: int = 1000, ttl_seconds: int = 900) -> None:
        self._cache: TTLCache[str, Any] = TTLCache(maxsize=maxsize, ttl=ttl_seconds)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            return self._cache.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._cache[key] = value

    def clear(self) -> None:
        """Remove all entries. Used by test fixtures."""
        self._cache.clear()

    @staticmethod
    def make_key(provider: str, query: str, max_results: int) -> str:
        return f"{provider}:{' '.join(query.split()).lower()}:{max_results}"
