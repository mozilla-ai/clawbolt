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
from backend.app.models import Contractor

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base for file storage providers.

    Each implementation handles a single storage destination (Dropbox, Google
    Drive, local filesystem, etc.).  The interface is intentionally minimal so
    that adding new backends is straightforward.

    Per-contractor isolation: when a contractor_id is provided, each backend
    isolates files into a per-contractor subdirectory (local) or path prefix
    (cloud).  A future Phase 2 will add shared cloud folders per contractor.
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
    def __init__(self, access_token: str, contractor_id: int | None = None) -> None:
        self.dbx = dropbox.Dropbox(access_token)
        self._path_prefix = f"/{contractor_id}" if contractor_id is not None else ""

    def _prefixed(self, path: str) -> str:
        """Prepend the per-contractor prefix to a Dropbox path."""
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
            logger.info("Dropbox upload complete: %s (no shared link)", full_path)
            return full_path

    async def create_folder(self, path: str) -> str:
        prefixed = self._prefixed(path)
        with contextlib.suppress(dropbox.exceptions.ApiError):
            await asyncio.to_thread(self.dbx.files_create_folder_v2, prefixed)
        return prefixed

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        src = f"{from_path}/{from_filename}"
        dest = f"{to_path}/{to_filename}"
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
            return dest

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        result = await asyncio.to_thread(self.dbx.files_list_folder, self._prefixed(path))
        files: list[dict[str, str]] = []
        for entry in result.entries:
            files.append({"name": entry.name, "path": entry.path_display})
        return files


class GoogleDriveStorage(StorageBackend):
    # TODO(Phase 2): Google Drive uses folder IDs, not path strings, so the
    # _path_prefix approach does not provide real per-contractor isolation.
    # Proper isolation requires creating a per-contractor root folder on first
    # use and passing its folder ID as the parent for all operations.

    def __init__(self, credentials_json: str, contractor_id: int | None = None) -> None:
        self.credentials_json = credentials_json
        self._service: Any = None
        self._path_prefix = f"{contractor_id}/" if contractor_id is not None else ""

    def _get_service(self) -> Any:
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_info(json.loads(self.credentials_json))
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        # TODO(Phase 2): Does not apply contractor isolation. Drive needs a
        # per-contractor root folder ID as the parent. See class-level TODO.
        from googleapiclient.http import MediaIoBaseUpload

        logger.info("Uploading to Google Drive: %s/%s (%d bytes)", path, filename, len(file_bytes))
        service = self._get_service()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/octet-stream")
        file_metadata = {"name": filename, "parents": [path] if path else []}
        result = await asyncio.to_thread(
            service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute
        )
        url = result.get("webViewLink", result.get("id", ""))
        logger.info("Google Drive upload complete: %s/%s -> %s", path, filename, url)
        return url

    async def create_folder(self, path: str) -> str:
        # TODO(Phase 2): The prefix logic here does not actually isolate folder
        # names. For path="/Job Photos" with contractor_id=42, prefixed becomes
        # "42/Job Photos" and split("/")[-1] still yields "Job Photos"
        # (unchanged). Real isolation needs a per-contractor root folder ID as
        # the parent. See class-level TODO.
        service = self._get_service()
        prefixed = f"{self._path_prefix}{path}" if self._path_prefix else path
        folder_metadata = {
            "name": prefixed.split("/")[-1],
            "mimeType": "application/vnd.google-apps.folder",
        }
        result = await asyncio.to_thread(
            service.files().create(body=folder_metadata, fields="id").execute
        )
        return result.get("id", "")

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        service = self._get_service()
        # Search for the file by name in the source folder
        query = f"name='{from_filename}' and '{from_path}' in parents and trashed=false"
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id,name)").execute
        )
        files = result.get("files", [])
        if not files:
            msg = f"File not found: {from_filename} in {from_path}"
            raise FileNotFoundError(msg)
        file_id = files[0]["id"]
        # Move to new parent and rename
        update_result = await asyncio.to_thread(
            service.files()
            .update(
                fileId=file_id,
                body={"name": to_filename},
                addParents=to_path,
                removeParents=from_path,
                fields="id,webViewLink",
            )
            .execute
        )
        return update_result.get("webViewLink", update_result.get("id", ""))

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        # TODO(Phase 2): Does not apply contractor isolation. Drive queries
        # folders by ID, not path. See class-level TODO.
        service = self._get_service()
        query = f"'{path}' in parents and trashed=false"
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id,name,webViewLink)").execute
        )
        files: list[dict[str, str]] = []
        for f in result.get("files", []):
            files.append({"name": f["name"], "path": f.get("webViewLink", f["id"])})
        return files


class LocalFileStorage(StorageBackend):
    """Local filesystem storage for development and demos."""

    def __init__(
        self,
        base_dir: str = settings.file_storage_base_dir,
        contractor_id: int | None = None,
    ) -> None:
        base = Path(base_dir).resolve()
        if contractor_id is not None:
            base = base / str(contractor_id)
        self.base_dir = base
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, *segments: str) -> Path:
        """Resolve path segments under base_dir, rejecting traversal attempts."""
        result = self.base_dir
        for seg in segments:
            result = result / seg.lstrip("/")
        resolved = result.resolve()
        if not str(resolved).startswith(str(self.base_dir)):
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
    contractor: Contractor | None = None,
) -> StorageBackend:
    """Factory: return the configured storage backend.

    Args:
        svc_settings: Override the global settings (useful in tests).
        contractor: When provided, files are isolated into a per-contractor
            subdirectory (local) or path prefix (cloud).
    """
    s = svc_settings or settings
    cid = contractor.id if contractor is not None else None
    if s.storage_provider == "local":
        return LocalFileStorage(base_dir=s.file_storage_base_dir, contractor_id=cid)
    elif s.storage_provider == "dropbox":
        return DropboxStorage(s.dropbox_access_token, contractor_id=cid)
    elif s.storage_provider == "google_drive":
        return GoogleDriveStorage(s.google_drive_credentials_json, contractor_id=cid)
    else:
        msg = f"Unknown storage provider: {s.storage_provider}"
        raise ValueError(msg)
