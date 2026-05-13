"""Helpers for resolving saved-media references against the storage backend.

After dropping the ``media_files`` manifest, the backend itself (Google
Drive) is the source of truth. The agent quotes storage paths
(``/Astro Home Management - 123 Main Street/photos/foo.jpg``) and these
helpers translate that string into bytes plus the metadata other
flows (vision, CompanyCam, AppFolio) need.
"""

from __future__ import annotations

import logging

from backend.app.services.storage_service import SavedFile, StorageBackend

logger = logging.getLogger(__name__)


def _looks_like_storage_path(ref: str) -> bool:
    """Heuristic for "the agent quoted a storage path, not a tunnel URL".

    We match anything that starts with ``/`` (the canonical form
    ``find_saved_files`` and ``upload_to_storage`` emit). We deliberately
    do NOT try to parse Drive ``webViewLink`` URLs: the agent should
    quote paths so cross-tool resolution stays predictable.
    """
    return ref.startswith("/")


async def find_saved_file(storage: StorageBackend, file_ref: str) -> SavedFile | None:
    """Resolve a saved-file reference to backend metadata.

    *file_ref* is either a storage path (``/Foo/bar.jpg``) or a filename
    fragment to search for. Path lookups are direct; non-path refs fall
    back to a backend search and pick the first match.
    """
    ref = file_ref.strip()
    if not ref:
        return None
    if _looks_like_storage_path(ref):
        meta = await storage.get_file(ref)
        if meta is not None:
            return meta
    matches = await storage.search_files(query=ref, limit=1)
    if matches:
        return matches[0]
    return None


async def latest_saved_file(storage: StorageBackend) -> SavedFile | None:
    """Return the most recently saved file, or None if storage is empty."""
    matches = await storage.search_files(query="", limit=1)
    return matches[0] if matches else None


async def read_saved_file_bytes(storage: StorageBackend, saved: SavedFile) -> bytes:
    """Load durable bytes for a saved file from the configured backend."""
    if not saved.path:
        msg = f"Saved file {saved.name!r} has no storage path."
        raise FileNotFoundError(msg)
    return await storage.download_file(saved.path)
