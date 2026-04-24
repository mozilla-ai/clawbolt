"""Bounded in-memory TTL cache for supplier API responses."""

import asyncio
from typing import Any

from cachetools import TTLCache


class SupplierCache:
    """Thread-safe TTL cache for supplier product data.

    Keyed by (supplier, normalized_query, zip_code). Default TTL is 4 hours.
    Max 2000 entries to prevent unbounded memory growth.
    """

    def __init__(self, maxsize: int = 2000, ttl_seconds: int = 14400) -> None:
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
    def make_key(supplier: str, query: str, zip_code: str) -> str:
        return f"{supplier}:{query.strip().lower()}:{zip_code}"
