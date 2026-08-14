"""In-process cache for unwrapped DEKs.

KMS Decrypt is the single most expensive call in the read path: 10-50ms
per round trip plus per-request cost. Almost every credential read
fetches the same wrapped DEK over and over (one per row, but rows are
read repeatedly). Caching the unwrapped DEK keyed by the wrapped bytes
turns those repeated reads into in-memory dict lookups.

The cache is process-local. Multi-replica deployments don't need
coordination because the cache holds a derived value: any replica that
can call KMS at all can re-populate the entry.
"""

from __future__ import annotations

import time
from threading import Lock


class DEKCache:
    """Bounded TTL cache for unwrapped DEKs.

    Keyed by the wrapped DEK bytes (which uniquely identify the row's
    DEK). Values are the unwrapped DEK plus an absolute expiry time.
    Eviction is lazy: expired entries are removed on access. When the
    cache hits ``max_entries``, the soonest-to-expire entry is evicted
    on the next ``put``.
    """

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 1000) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[bytes, tuple[bytes, float]] = {}
        self._lock = Lock()

    def get(self, wrapped_dek: bytes) -> bytes | None:
        with self._lock:
            entry = self._store.get(wrapped_dek)
            if entry is None:
                return None
            dek, expires_at = entry
            if time.monotonic() >= expires_at:
                self._store.pop(wrapped_dek, None)
                return None
            return dek

    def put(self, wrapped_dek: bytes, dek: bytes) -> None:
        with self._lock:
            if len(self._store) >= self._max and wrapped_dek not in self._store:
                # Drop the entry expiring soonest. O(n) but n is small
                # (max_entries default 1000) and cache misses are rare
                # once warm.
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                self._store.pop(oldest_key, None)
            self._store[wrapped_dek] = (dek, time.monotonic() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)
