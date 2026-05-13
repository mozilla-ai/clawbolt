from __future__ import annotations

import asyncio
import io
import logging
import random
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# googleapiclient ``Resource`` and its underlying ``httplib2.Http`` are not
# thread-safe (see googleapis/google-api-python-client docs/thread_safety.md).
# Two ``.execute()`` calls racing on the same Resource interleave writes on
# one TLS socket and surface as ``ssl.SSLError: record layer failure`` or
# ``TimeoutError: read operation timed out`` from the other thread. The fix
# is to build a fresh Resource per call (no cross-call sharing) and to
# retry the transient error families on a small backoff in case a genuine
# network blip slips through.
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_DELAY_S = 0.5
_TRANSIENT_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
)


# Top-level folder created in the user's Drive to namespace app-managed
# files. ``drive.file`` scope means the integration only sees files it
# created, so this folder is invisible to other apps and won't clash with
# the user's existing Drive contents.
ROOT_FOLDER_NAME = "Clawbolt"


@dataclass
class SavedFile:
    """Metadata for a file persisted to per-user storage.

    Fields are derived from the backend's native metadata, not from a
    Clawbolt-side shadow table. ``path`` is the human-readable storage
    path the agent quotes across turns and tools (e.g.
    ``/Astro Home Management - 123 Penn Ave/photos/foo.jpg``).
    """

    path: str
    name: str
    mime_type: str = ""
    description: str = ""
    web_view_link: str = ""
    modified_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class StorageBackend(ABC):
    """Abstract base for per-user file storage backends.

    The product currently has one concrete implementation
    (:class:`GoogleDriveStorage`); the interface stays minimal and async so
    additional per-user OAuth backends can slot in without changes to call
    sites.
    """

    @abstractmethod
    async def upload_file(
        self,
        file_bytes: bytes,
        path: str,
        filename: str,
        *,
        mime_type: str = "application/octet-stream",
        description: str = "",
    ) -> SavedFile:
        """Upload a file. Returns metadata for the uploaded file."""

    @abstractmethod
    async def create_folder(self, path: str) -> str:
        """Create a folder. Returns the folder path."""

    @abstractmethod
    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> SavedFile:
        """Move/rename a file. Returns metadata for the moved file."""

    @abstractmethod
    async def list_folder(self, path: str) -> list[SavedFile]:
        """List files in a folder."""

    @abstractmethod
    async def download_file(self, path: str) -> bytes:
        """Download a previously stored file by its logical storage path."""

    @abstractmethod
    async def get_file(self, path: str) -> SavedFile | None:
        """Fetch metadata for a single file by storage path. Returns None if missing."""

    @abstractmethod
    async def search_files(self, query: str = "", limit: int = 10) -> list[SavedFile]:
        """Return up to *limit* files matching *query*.

        With an empty query, returns the most recently modified files
        the backend knows about. Matching covers filename and the
        backend's stored description field.
        """


@dataclass
class DriveOAuthCredentials:
    """Minimal token bundle the Drive client needs for auto-refresh.

    ``client_id`` / ``client_secret`` are the deployment-level OAuth client
    credentials; ``access_token`` / ``refresh_token`` are the per-user
    tokens issued by Google after the user grants ``drive.file`` scope.
    """

    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str


# appProperties key holding the human-readable storage path. drive.file
# scope means the app only sees files it uploaded, so no namespacing
# beyond the key itself is needed.
_DRIVE_PATH_PROPERTY = "clawbolt_path"

_DRIVE_FILE_FIELDS = "id,name,mimeType,description,webViewLink,modifiedTime,parents,appProperties"


class GoogleDriveStorage(StorageBackend):
    """Google Drive storage scoped to a single user's own Drive.

    Each user grants ``drive.file`` scope through the integration OAuth
    flow; files land in the user's own Drive under a top-level
    :data:`ROOT_FOLDER_NAME` folder. The ``drive.file`` scope means the
    integration only sees files it created, so the namespace is implicit.

    Folder lookups are cached in-process for the lifetime of the backend
    instance, which is rebuilt every turn.
    """

    def __init__(self, credentials: DriveOAuthCredentials) -> None:
        self._credentials = credentials
        # ``_service`` is a test-only override. Production leaves it ``None``
        # and ``_get_service`` builds a fresh Resource on every call. The
        # cross-call shared Resource was the root cause of the SSL/timeout
        # storm in the Durham-receipts incident: one ``Resource`` wraps one
        # ``httplib2.Http`` which wraps one TLS socket, and the agent fans
        # uploads out concurrently via ``asyncio.to_thread``.
        self._service: Any = None
        self._folder_cache: dict[str, str] = {}

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        return self._build_service()

    def _build_service(self) -> Any:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=self._credentials.access_token,
            refresh_token=self._credentials.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._credentials.client_id,
            client_secret=self._credentials.client_secret,
        )
        # ``cache_discovery=False`` silences the file-cache deprecation
        # warning; the discovery document is small enough that the network
        # fetch on first call is cached in-process for the rest of the
        # interpreter's lifetime.
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    async def _execute_with_retry(
        self,
        build_request: Callable[[], Any],
        *,
        op: str,
    ) -> Any:
        """Run a googleapiclient request with bounded backoff on transient errors.

        ``build_request`` is invoked fresh on every attempt so the
        underlying ``HttpRequest`` (and any ``MediaIoBaseUpload`` it
        carries) is reconstructed rather than reused after a partial
        failure. Only ``ssl.SSLError`` / ``ConnectionError`` /
        ``TimeoutError`` are retried; ``HttpError`` (HTTP-level
        failures like 4xx/5xx) is left to the caller.
        """
        last_exc: BaseException | None = None
        for attempt in range(_TRANSIENT_RETRY_ATTEMPTS):
            try:
                request = build_request()
                return await asyncio.to_thread(request.execute)
            except _TRANSIENT_RETRY_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == _TRANSIENT_RETRY_ATTEMPTS - 1:
                    break
                delay = _TRANSIENT_RETRY_BASE_DELAY_S * (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "Transient Drive error on %s (attempt %d/%d): %s; retrying in %.2fs",
                    op,
                    attempt + 1,
                    _TRANSIENT_RETRY_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def _find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        """Return the folder id for *name* under *parent_id*, if it exists."""
        service = self._get_service()
        safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
        parent_clause = f"'{parent_id}' in parents" if parent_id else "'root' in parents"
        query = (
            f"name='{safe_name}' and {parent_clause} "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        result = await self._execute_with_retry(
            lambda: service.files().list(q=query, fields="files(id)", pageSize=1),
            op="find_folder",
        )
        existing = result.get("files", [])
        if existing:
            return existing[0]["id"]
        return None

    async def _find_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find an existing folder by *name* under *parent_id*, or create one."""
        service = self._get_service()
        existing_id = await self._find_folder(name, parent_id)
        if existing_id is not None:
            return existing_id
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        created = await self._execute_with_retry(
            lambda: service.files().create(body=metadata, fields="id"),
            op="create_folder",
        )
        return created["id"]

    async def _resolve_path(self, path: str) -> str:
        """Translate a human-readable path to a Google Drive folder ID.

        Creates intermediate folders as needed.  For example,
        ``/Unsorted/2026-03-02`` ensures the app root folder, then
        ``Unsorted``, then ``2026-03-02`` under it.
        """
        parts = [p for p in path.strip("/").split("/") if p]

        root_key = ROOT_FOLDER_NAME
        if root_key not in self._folder_cache:
            self._folder_cache[root_key] = await self._find_or_create_folder(ROOT_FOLDER_NAME)
        current_id: str = self._folder_cache[root_key]
        current_path = ROOT_FOLDER_NAME

        if not parts:
            return current_id

        for part in parts:
            cache_key = f"{current_path}/{part}"
            if cache_key not in self._folder_cache:
                self._folder_cache[cache_key] = await self._find_or_create_folder(part, current_id)
            current_id = self._folder_cache[cache_key]
            current_path = cache_key

        return current_id

    async def _resolve_existing_path(self, path: str) -> str | None:
        """Translate a human-readable path to an existing Google Drive folder ID."""
        parts = [p for p in path.strip("/").split("/") if p]

        root_key = ROOT_FOLDER_NAME
        current_id = self._folder_cache.get(root_key)
        if current_id is None:
            current_id = await self._find_folder(ROOT_FOLDER_NAME)
            if current_id is None:
                return None
            self._folder_cache[root_key] = current_id
        current_path = ROOT_FOLDER_NAME

        if not parts:
            return current_id

        for part in parts:
            cache_key = f"{current_path}/{part}"
            next_id = self._folder_cache.get(cache_key)
            if next_id is None:
                next_id = await self._find_folder(part, current_id)
                if next_id is None:
                    return None
                self._folder_cache[cache_key] = next_id
            current_id = next_id
            current_path = cache_key

        return current_id

    @staticmethod
    def _normalized_path(folder_path: str, filename: str) -> str:
        return "/" + "/".join(p for p in (folder_path.strip("/"), filename.strip("/")) if p)

    @classmethod
    def _from_drive_file(cls, payload: dict[str, Any], fallback_path: str = "") -> SavedFile:
        app_props = payload.get("appProperties") or {}
        path = app_props.get(_DRIVE_PATH_PROPERTY) or fallback_path
        return SavedFile(
            path=path,
            name=payload.get("name", ""),
            mime_type=payload.get("mimeType", ""),
            description=payload.get("description", "") or "",
            web_view_link=payload.get("webViewLink", "") or "",
            modified_at=payload.get("modifiedTime", "") or "",
            metadata={"id": payload.get("id", "")},
        )

    async def upload_file(
        self,
        file_bytes: bytes,
        path: str,
        filename: str,
        *,
        mime_type: str = "application/octet-stream",
        description: str = "",
    ) -> SavedFile:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload

        logger.info("Uploading to Google Drive: %s/%s (%d bytes)", path, filename, len(file_bytes))
        folder_id = await self._resolve_path(path)
        service = self._get_service()
        storage_path = self._normalized_path(path, filename)
        file_metadata: dict[str, Any] = {
            "name": filename,
            "parents": [folder_id],
            "appProperties": {_DRIVE_PATH_PROPERTY: storage_path},
        }
        if description:
            file_metadata["description"] = description

        # Build the request fresh on each retry. ``MediaIoBaseUpload``
        # consumes its ``BytesIO`` source as the upload streams, so a
        # partial-write failure would leave a reused instance at the wrong
        # offset on the second attempt.
        def _build() -> Any:
            media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type)
            return service.files().create(
                body=file_metadata, media_body=media, fields=_DRIVE_FILE_FIELDS
            )

        try:
            result = await self._execute_with_retry(_build, op="upload_file")
        except HttpError as exc:
            logger.exception("Google Drive upload failed: %s/%s", path, filename)
            msg = f"Google Drive upload failed for {path}/{filename}: {exc}"
            raise RuntimeError(msg) from exc
        except _TRANSIENT_RETRY_EXCEPTIONS as exc:
            logger.exception(
                "Google Drive upload failed after %d transient retries: %s/%s",
                _TRANSIENT_RETRY_ATTEMPTS,
                path,
                filename,
            )
            msg = f"Google Drive upload failed for {path}/{filename}: {exc}"
            raise RuntimeError(msg) from exc
        saved = self._from_drive_file(result, fallback_path=storage_path)
        logger.info(
            "Google Drive upload complete: %s -> %s",
            saved.path,
            saved.web_view_link or saved.metadata.get("id", ""),
        )
        return saved

    async def create_folder(self, path: str) -> str:
        return await self._resolve_path(path)

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> SavedFile:
        from_folder_id = await self._resolve_path(from_path)
        to_folder_id = await self._resolve_path(to_path)
        service = self._get_service()
        safe_name = from_filename.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{safe_name}' and '{from_folder_id}' in parents and trashed=false"
        result = await self._execute_with_retry(
            lambda: service.files().list(q=query, fields="files(id,name)"),
            op="move_file.list",
        )
        files = result.get("files", [])
        if not files:
            msg = f"File not found: {from_filename} in {from_path}"
            raise FileNotFoundError(msg)
        file_id = files[0]["id"]
        new_path = self._normalized_path(to_path, to_filename)
        update_result = await self._execute_with_retry(
            lambda: service.files().update(
                fileId=file_id,
                body={
                    "name": to_filename,
                    "appProperties": {_DRIVE_PATH_PROPERTY: new_path},
                },
                addParents=to_folder_id,
                removeParents=from_folder_id,
                fields=_DRIVE_FILE_FIELDS,
            ),
            op="move_file.update",
        )
        return self._from_drive_file(update_result, fallback_path=new_path)

    async def list_folder(self, path: str) -> list[SavedFile]:
        folder_id = await self._resolve_existing_path(path)
        if folder_id is None:
            return []
        service = self._get_service()
        query = f"'{folder_id}' in parents and trashed=false"
        result = await self._execute_with_retry(
            lambda: service.files().list(
                q=query, fields=f"files({_DRIVE_FILE_FIELDS})", pageSize=200
            ),
            op="list_folder",
        )
        normalized_path = path.strip("/")
        files: list[SavedFile] = []
        for payload in result.get("files", []):
            fallback = "/" + "/".join(p for p in (normalized_path, payload.get("name", "")) if p)
            files.append(self._from_drive_file(payload, fallback_path=fallback))
        return files

    async def get_file(self, path: str) -> SavedFile | None:
        normalized = path.strip("/")
        if not normalized or "/" not in normalized:
            folder_path, filename = "", normalized
        else:
            folder_path, filename = normalized.rsplit("/", 1)
        folder_id = await self._resolve_existing_path(folder_path)
        if folder_id is None:
            return None
        service = self._get_service()
        safe_name = filename.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
        result = await self._execute_with_retry(
            lambda: service.files().list(
                q=query, fields=f"files({_DRIVE_FILE_FIELDS})", pageSize=1
            ),
            op="get_file",
        )
        files = result.get("files", [])
        if not files:
            return None
        return self._from_drive_file(files[0], fallback_path=f"/{normalized}")

    async def search_files(self, query: str = "", limit: int = 10) -> list[SavedFile]:
        bounded = max(1, min(limit, 100))
        service = self._get_service()
        clauses = ["trashed=false", "mimeType!='application/vnd.google-apps.folder'"]
        for token in _search_tokens(query):
            safe = token.replace("\\", "\\\\").replace("'", "\\'")
            clauses.append(f"(name contains '{safe}' or fullText contains '{safe}')")
        q = " and ".join(clauses)
        result = await self._execute_with_retry(
            lambda: service.files().list(
                q=q,
                fields=f"files({_DRIVE_FILE_FIELDS})",
                pageSize=bounded,
                orderBy="modifiedTime desc",
            ),
            op="search_files",
        )
        return [self._from_drive_file(payload) for payload in result.get("files", [])]

    async def download_file(self, path: str) -> bytes:
        from googleapiclient.errors import HttpError

        normalized = path.strip("/")
        if not normalized:
            msg = "Cannot download a folder path from Google Drive."
            raise FileNotFoundError(msg)

        folder_path, filename = normalized.rsplit("/", 1) if "/" in normalized else ("", normalized)
        folder_id = await self._resolve_existing_path(folder_path)
        if folder_id is None:
            msg = f"Google Drive folder not found for {path!r}"
            raise FileNotFoundError(msg)

        service = self._get_service()
        safe_name = filename.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{safe_name}' and '{folder_id}' in parents and trashed=false"
        result = await self._execute_with_retry(
            lambda: service.files().list(q=query, fields="files(id)", pageSize=1),
            op="download_file.list",
        )
        files = result.get("files", [])
        if not files:
            msg = f"File not found in Google Drive: {path}"
            raise FileNotFoundError(msg)

        file_id = files[0]["id"]
        try:
            data = await self._execute_with_retry(
                lambda: service.files().get_media(fileId=file_id),
                op="download_file.get_media",
            )
        except HttpError as exc:
            msg = f"Google Drive download failed for {path}: {exc}"
            raise RuntimeError(msg) from exc
        except _TRANSIENT_RETRY_EXCEPTIONS as exc:
            msg = f"Google Drive download failed for {path}: {exc}"
            raise RuntimeError(msg) from exc
        return bytes(data)


def _search_tokens(query: str) -> list[str]:
    """Tokenize a search string for case-insensitive Drive lookup."""
    import re

    return [token for token in re.split(r"\W+", query.strip()) if token]
