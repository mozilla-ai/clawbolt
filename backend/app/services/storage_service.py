from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import dropbox
import dropbox.exceptions
import dropbox.files

from backend.app.config import Settings, settings
from backend.app.models import User

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base for file storage providers.

    Each implementation handles a single storage destination (Dropbox, Google
    Drive, local filesystem, etc.).  The interface is intentionally minimal so
    that adding new backends is straightforward.

    Per-user isolation: when a user_id is provided, each backend
    isolates files into a per-user subdirectory (local) or path prefix
    (cloud).  A future Phase 2 will add shared cloud folders per user.
    """

    @abstractmethod
    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        """Upload a file. Returns the public/shared URL."""

    @abstractmethod
    async def create_folder(self, path: str) -> str:
        """Create a folder. Returns the folder path."""

    @abstractmethod
    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        """Move/rename a file. Returns the new URL/path."""

    @abstractmethod
    async def list_folder(self, path: str) -> list[dict[str, str]]:
        """List files in a folder. Returns list of file metadata."""


class DropboxStorage(StorageBackend):
    def __init__(self, access_token: str, user_id: str | None = None) -> None:
        self.dbx = dropbox.Dropbox(access_token)
        self._path_prefix = f"/{user_id}" if user_id is not None else ""

    def _prefixed(self, path: str) -> str:
        """Prepend the per-user prefix to a Dropbox path."""
        return f"{self._path_prefix}{path}" if self._path_prefix else path

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        full_path = f"{self._prefixed(path)}/{filename}"
        logger.info("Uploading to Dropbox: %s (%d bytes)", full_path, len(file_bytes))
        await asyncio.to_thread(
            self.dbx.files_upload, file_bytes, full_path, mode=dropbox.files.WriteMode.overwrite
        )
        # Create a shared link
        try:
            shared = await asyncio.to_thread(
                self.dbx.sharing_create_shared_link_with_settings, full_path
            )
            logger.info("Dropbox upload complete: %s -> %s", full_path, shared.url)
            return shared.url
        except dropbox.exceptions.ApiError:
            # Link may already exist
            links = await asyncio.to_thread(self.dbx.sharing_list_shared_links, path=full_path)
            if links.links:
                logger.info("Dropbox upload complete: %s -> %s", full_path, links.links[0].url)
                return links.links[0].url
            logger.warning("Dropbox upload: no shared link available for %s", full_path)
            return full_path

    async def create_folder(self, path: str) -> str:
        prefixed = self._prefixed(path)
        with contextlib.suppress(dropbox.exceptions.ApiError):
            await asyncio.to_thread(self.dbx.files_create_folder_v2, prefixed)
        return prefixed

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        src = self._prefixed(f"{from_path}/{from_filename}")
        dest = self._prefixed(f"{to_path}/{to_filename}")
        logger.info("Moving file in Dropbox: %s -> %s", src, dest)
        await asyncio.to_thread(self.dbx.files_move_v2, src, dest)
        # Create a shared link for the new location
        try:
            shared = await asyncio.to_thread(
                self.dbx.sharing_create_shared_link_with_settings, dest
            )
            return shared.url
        except dropbox.exceptions.ApiError:
            links = await asyncio.to_thread(self.dbx.sharing_list_shared_links, path=dest)
            if links.links:
                return links.links[0].url
            logger.warning("Dropbox move: no shared link available for %s", dest)
            return dest

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        result = await asyncio.to_thread(self.dbx.files_list_folder, self._prefixed(path))
        files: list[dict[str, str]] = []
        for entry in result.entries:
            files.append({"name": entry.name, "path": entry.path_display})
        return files


class GoogleDriveStorage(StorageBackend):
    """Google Drive storage backend with per-user folder isolation.

    All paths are human-readable strings (e.g. "/Unsorted/2026-03-02") that get
    resolved to Google Drive folder IDs automatically.  When a *user_id* is set,
    a per-user root folder is created in Drive and all paths are nested inside it.
    """

    def __init__(self, credentials_json: str, user_id: str | None = None) -> None:
        self.credentials_json = credentials_json
        self._service: Any = None
        self._user_id = user_id
        self._folder_cache: dict[str, str] = {}

    def _get_service(self) -> Any:
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_info(json.loads(self.credentials_json))
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    async def _find_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find an existing folder by *name* under *parent_id*, or create one."""
        service = self._get_service()
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        parent_clause = f"'{parent_id}' in parents" if parent_id else "'root' in parents"
        query = (
            f"name='{safe_name}' and {parent_clause} "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id)", pageSize=1).execute
        )
        existing = result.get("files", [])
        if existing:
            return existing[0]["id"]
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        created = await asyncio.to_thread(
            service.files().create(body=metadata, fields="id").execute
        )
        return created["id"]

    async def _resolve_path(self, path: str) -> str:
        """Translate a human-readable path to a Google Drive folder ID.

        Creates intermediate folders as needed.  For example,
        ``/Unsorted/2026-03-02`` first ensures an ``Unsorted`` folder exists,
        then ensures ``2026-03-02`` exists inside it.  When *user_id* is set the
        entire tree is nested under a per-user root folder.
        """
        parts = [p for p in path.strip("/").split("/") if p]
        if self._user_id:
            root_key = self._user_id
            if root_key not in self._folder_cache:
                self._folder_cache[root_key] = await self._find_or_create_folder(self._user_id)
            current_id: str | None = self._folder_cache[root_key]
            current_path = self._user_id
        else:
            current_id = None
            current_path = ""

        if not parts:
            return current_id or "root"

        for part in parts:
            cache_key = f"{current_path}/{part}" if current_path else part
            if cache_key not in self._folder_cache:
                self._folder_cache[cache_key] = await self._find_or_create_folder(part, current_id)
            current_id = self._folder_cache[cache_key]
            current_path = cache_key

        return current_id  # type: ignore[return-value]

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload

        logger.info("Uploading to Google Drive: %s/%s (%d bytes)", path, filename, len(file_bytes))
        folder_id = await self._resolve_path(path)
        service = self._get_service()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/octet-stream")
        file_metadata: dict[str, Any] = {"name": filename, "parents": [folder_id]}
        try:
            result = await asyncio.to_thread(
                service.files()
                .create(body=file_metadata, media_body=media, fields="id,webViewLink")
                .execute
            )
        except HttpError as exc:
            logger.exception("Google Drive upload failed: %s/%s", path, filename)
            msg = f"Google Drive upload failed for {path}/{filename}: {exc}"
            raise RuntimeError(msg) from exc
        url = result.get("webViewLink", result.get("id", ""))
        logger.info("Google Drive upload complete: %s/%s -> %s", path, filename, url)
        return url

    async def create_folder(self, path: str) -> str:
        return await self._resolve_path(path)

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        from_folder_id = await self._resolve_path(from_path)
        to_folder_id = await self._resolve_path(to_path)
        service = self._get_service()
        safe_name = from_filename.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{safe_name}' and '{from_folder_id}' in parents and trashed=false"
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id,name)").execute
        )
        files = result.get("files", [])
        if not files:
            msg = f"File not found: {from_filename} in {from_path}"
            raise FileNotFoundError(msg)
        file_id = files[0]["id"]
        update_result = await asyncio.to_thread(
            service.files()
            .update(
                fileId=file_id,
                body={"name": to_filename},
                addParents=to_folder_id,
                removeParents=from_folder_id,
                fields="id,webViewLink",
            )
            .execute
        )
        return update_result.get("webViewLink", update_result.get("id", ""))

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        folder_id = await self._resolve_path(path)
        service = self._get_service()
        query = f"'{folder_id}' in parents and trashed=false"
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id,name,webViewLink)").execute
        )
        return [
            {"name": f["name"], "path": f.get("webViewLink", f["id"])}
            for f in result.get("files", [])
        ]


class LocalFileStorage(StorageBackend):
    """Local filesystem storage for development and demos."""

    def __init__(
        self,
        base_dir: str = settings.file_storage_base_dir,
        user_id: str | None = None,
    ) -> None:
        base = Path(base_dir).resolve()
        if user_id is not None:
            base = base / str(user_id)
        self.base_dir = base
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, *segments: str) -> Path:
        """Resolve path segments under base_dir, rejecting traversal attempts."""
        result = self.base_dir
        for seg in segments:
            result = result / seg.lstrip("/")
        resolved = result.resolve()
        if not resolved.is_relative_to(self.base_dir):
            msg = f"Path escapes storage directory: {'/'.join(segments)}"
            raise ValueError(msg)
        return resolved

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        file_path = self._safe_path(path, filename)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Saving to local storage: %s (%d bytes)", file_path, len(file_bytes))
        await asyncio.to_thread(file_path.write_bytes, file_bytes)
        return f"file://{file_path}"

    async def create_folder(self, path: str) -> str:
        folder = self._safe_path(path)
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        src = self._safe_path(from_path, from_filename)
        dest = self._safe_path(to_path, to_filename)
        if not src.exists():
            msg = f"Source file not found: {from_path}/{from_filename}"
            raise FileNotFoundError(msg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Moving local file: %s -> %s", src, dest)
        await asyncio.to_thread(src.rename, dest)
        return f"file://{dest}"

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        folder = self._safe_path(path)
        if not folder.exists():
            return []
        return [{"name": f.name, "path": str(f)} for f in folder.iterdir() if f.is_file()]


def get_storage_service(
    svc_settings: Settings | None = None,
    user: User | None = None,
) -> StorageBackend:
    """Factory: return the configured storage backend.

    Args:
        svc_settings: Override the global settings (useful in tests).
        user: When provided, files are isolated into a per-user
            subdirectory (local) or path prefix (cloud).
    """
    s = svc_settings or settings
    cid = user.id if user is not None else None
    if s.storage_provider == "local":
        return LocalFileStorage(base_dir=s.file_storage_base_dir, user_id=cid)
    elif s.storage_provider == "dropbox":
        return DropboxStorage(s.dropbox_access_token, user_id=cid)
    elif s.storage_provider == "google_drive":
        return GoogleDriveStorage(s.google_drive_credentials_json, user_id=cid)
    else:
        msg = f"Unknown storage provider: {s.storage_provider}"
        raise ValueError(msg)
