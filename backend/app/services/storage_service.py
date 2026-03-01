from __future__ import annotations

import asyncio
import contextlib
import io
import json
from abc import ABC, abstractmethod
from pathlib import Path

import dropbox

from backend.app.config import Settings, settings


class StorageBackend(ABC):
    """Abstract base for file storage providers.

    Each implementation handles a single storage destination (Dropbox, Google
    Drive, local filesystem, etc.).  The interface is intentionally minimal so
    that adding new backends is straightforward.

    Multi-tenant note: today one backend instance is shared across all
    contractors.  A future iteration will allow per-contractor credentials so
    each contractor's files go to their own cloud account.
    """

    @abstractmethod
    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        """Upload a file. Returns the public/shared URL."""

    @abstractmethod
    async def create_folder(self, path: str) -> str:
        """Create a folder. Returns the folder path."""

    @abstractmethod
    async def list_folder(self, path: str) -> list[dict[str, str]]:
        """List files in a folder. Returns list of file metadata."""


class DropboxStorage(StorageBackend):
    def __init__(self, access_token: str) -> None:
        self.dbx = dropbox.Dropbox(access_token)

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        full_path = f"{path}/{filename}"
        await asyncio.to_thread(
            self.dbx.files_upload, file_bytes, full_path, mode=dropbox.files.WriteMode.overwrite
        )
        # Create a shared link
        try:
            shared = await asyncio.to_thread(
                self.dbx.sharing_create_shared_link_with_settings, full_path
            )
            return shared.url
        except dropbox.exceptions.ApiError:
            # Link may already exist
            links = await asyncio.to_thread(self.dbx.sharing_list_shared_links, path=full_path)
            if links.links:
                return links.links[0].url
            return full_path

    async def create_folder(self, path: str) -> str:
        with contextlib.suppress(dropbox.exceptions.ApiError):
            await asyncio.to_thread(self.dbx.files_create_folder_v2, path)
        return path

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        result = await asyncio.to_thread(self.dbx.files_list_folder, path)
        files: list[dict[str, str]] = []
        for entry in result.entries:
            files.append({"name": entry.name, "path": entry.path_display})
        return files


class GoogleDriveStorage(StorageBackend):
    def __init__(self, credentials_json: str) -> None:
        self.credentials_json = credentials_json
        self._service: object = None

    def _get_service(self) -> object:
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_info(json.loads(self.credentials_json))
            self._service = build("drive", "v3", credentials=creds)
        return self._service

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        service = self._get_service()
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/octet-stream")
        file_metadata = {"name": filename, "parents": [path] if path else []}
        result = await asyncio.to_thread(
            service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute  # type: ignore[union-attr]
        )
        return result.get("webViewLink", result.get("id", ""))

    async def create_folder(self, path: str) -> str:
        service = self._get_service()
        folder_metadata = {
            "name": path.split("/")[-1],
            "mimeType": "application/vnd.google-apps.folder",
        }
        result = await asyncio.to_thread(
            service.files().create(body=folder_metadata, fields="id").execute  # type: ignore[union-attr]
        )
        return result.get("id", "")

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        service = self._get_service()
        query = f"'{path}' in parents and trashed=false"
        result = await asyncio.to_thread(
            service.files().list(q=query, fields="files(id,name,webViewLink)").execute  # type: ignore[union-attr]
        )
        files: list[dict[str, str]] = []
        for f in result.get("files", []):
            files.append({"name": f["name"], "path": f.get("webViewLink", f["id"])})
        return files


class LocalFileStorage(StorageBackend):
    """Local filesystem storage for development and demos."""

    def __init__(self, base_dir: str = settings.file_storage_base_dir) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        folder = self.base_dir / path.lstrip("/")
        folder.mkdir(parents=True, exist_ok=True)
        file_path = folder / filename
        await asyncio.to_thread(file_path.write_bytes, file_bytes)
        return f"file://{file_path.resolve()}"

    async def create_folder(self, path: str) -> str:
        folder = self.base_dir / path.lstrip("/")
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        folder = self.base_dir / path.lstrip("/")
        if not folder.exists():
            return []
        return [{"name": f.name, "path": str(f)} for f in folder.iterdir() if f.is_file()]


def get_storage_service(
    svc_settings: Settings | None = None,
    contractor: object | None = None,
) -> StorageBackend:
    """Factory: return the configured storage backend.

    Args:
        svc_settings: Override the global settings (useful in tests).
        contractor: Reserved for future multi-tenant support where each
            contractor may have their own storage credentials.
    """
    s = svc_settings or settings
    if s.storage_provider == "local":
        return LocalFileStorage()
    elif s.storage_provider == "dropbox":
        return DropboxStorage(s.dropbox_access_token)
    elif s.storage_provider == "google_drive":
        return GoogleDriveStorage(s.google_drive_credentials_json)
    else:
        msg = f"Unknown storage provider: {s.storage_provider}"
        raise ValueError(msg)
