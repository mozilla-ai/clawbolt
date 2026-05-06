"""Helpers for durable saved-media lookup and retrieval."""

from __future__ import annotations

from backend.app.agent.dto import MediaData
from backend.app.agent.stores import MediaStore
from backend.app.services.storage_service import StorageBackend


async def find_saved_media_record(user_id: str, file_ref: str) -> MediaData | None:
    """Resolve a saved-media reference by durable id, storage URL, or storage path."""
    ref = file_ref.strip()
    if not ref:
        return None

    media_store = MediaStore(user_id)
    if ref.startswith("media-"):
        media = await media_store.get_by_id(ref)
        if media is not None:
            return media
    return await media_store.get_by_url(ref)


async def latest_saved_media_record(user_id: str) -> MediaData | None:
    """Return the most recently saved file for the user, if any."""
    media_store = MediaStore(user_id)
    recent = await media_store.search(limit=1)
    if not recent:
        return None
    return recent[0]


async def read_saved_media_bytes(storage: StorageBackend, media: MediaData) -> bytes:
    """Load durable bytes for a saved media record from the configured backend."""
    if not media.storage_path:
        ref = media.id or media.original_url or "<unknown>"
        msg = f"Saved media {ref} does not have a storage_path."
        raise FileNotFoundError(msg)
    return await storage.download_file(media.storage_path)
