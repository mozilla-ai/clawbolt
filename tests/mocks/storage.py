from __future__ import annotations

import datetime
import re

from backend.app.services.storage_service import SavedFile, StorageBackend


class MockStorageBackend(StorageBackend):
    """In-memory mock storage for testing."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.metadata: dict[str, SavedFile] = {}
        self.folders: list[str] = []

    @staticmethod
    def _normalized(path: str, filename: str = "") -> str:
        joined = "/".join(p for p in (path.strip("/"), filename.strip("/")) if p)
        return f"/{joined}" if joined else "/"

    def _make_metadata(
        self,
        path: str,
        filename: str,
        mime_type: str,
        description: str,
    ) -> SavedFile:
        full = self._normalized(path, filename)
        return SavedFile(
            path=full,
            name=filename,
            mime_type=mime_type,
            description=description,
            web_view_link=f"https://mock-storage.example.com{full}",
            modified_at=datetime.datetime.now(datetime.UTC).isoformat(),
            metadata={"id": full},
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
        full = self._normalized(path, filename)
        # Strip leading slash for the legacy ``files`` keying so existing
        # tests that look up ``f"{path}/{filename}"`` still resolve.
        legacy_key = full.lstrip("/")
        self.files[legacy_key] = file_bytes
        meta = self._make_metadata(path, filename, mime_type, description)
        self.metadata[full] = meta
        return meta

    async def create_folder(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path.strip('/')}"
        if normalized not in self.folders:
            self.folders.append(normalized)
        return normalized

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> SavedFile:
        src_legacy = f"{from_path.strip('/')}/{from_filename}".lstrip("/")
        if src_legacy not in self.files:
            msg = f"File not found: {src_legacy}"
            raise FileNotFoundError(msg)
        dest_legacy = f"{to_path.strip('/')}/{to_filename}".lstrip("/")
        self.files[dest_legacy] = self.files.pop(src_legacy)

        src_full = self._normalized(from_path, from_filename)
        dest_full = self._normalized(to_path, to_filename)
        old_meta = self.metadata.pop(src_full, None)
        new_meta = self._make_metadata(
            to_path,
            to_filename,
            mime_type=old_meta.mime_type if old_meta else "application/octet-stream",
            description=old_meta.description if old_meta else "",
        )
        self.metadata[dest_full] = new_meta
        return new_meta

    async def list_folder(self, path: str) -> list[SavedFile]:
        normalized = path.strip("/")
        prefix = f"{normalized}/" if normalized else ""
        out: list[SavedFile] = []
        for key in self.files:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix) :]
            if "/" in remainder:
                continue
            full = f"/{key}"
            meta = self.metadata.get(full)
            if meta is None:
                meta = SavedFile(
                    path=full,
                    name=remainder,
                    web_view_link=f"https://mock-storage.example.com{full}",
                )
            out.append(meta)
        return out

    async def download_file(self, path: str) -> bytes:
        legacy = path.lstrip("/")
        if legacy not in self.files:
            msg = f"File not found: {path}"
            raise FileNotFoundError(msg)
        return self.files[legacy]

    async def get_file(self, path: str) -> SavedFile | None:
        normalized = path if path.startswith("/") else f"/{path.strip('/')}"
        legacy = normalized.lstrip("/")
        if legacy not in self.files:
            return None
        meta = self.metadata.get(normalized)
        if meta is not None:
            return meta
        name = legacy.rsplit("/", 1)[-1]
        return SavedFile(
            path=normalized,
            name=name,
            web_view_link=f"https://mock-storage.example.com{normalized}",
        )

    async def update_file_content(
        self,
        path: str,
        file_bytes: bytes,
        *,
        mime_type: str = "text/plain",
    ) -> SavedFile:
        legacy = path.lstrip("/")
        if legacy not in self.files:
            msg = f"File not found: {path}"
            raise FileNotFoundError(msg)
        self.files[legacy] = file_bytes
        meta = self.metadata.get(path)
        if meta is None:
            name = legacy.rsplit("/", 1)[-1]
            meta = self._make_metadata(
                path.rsplit("/", 1)[0] if "/" in path.lstrip("/") else "",
                name,
                mime_type,
                description="",
            )
        else:
            meta.mime_type = mime_type
        return meta

    async def search_files(self, query: str = "", limit: int = 10) -> list[SavedFile]:
        tokens = [t.lower() for t in re.split(r"\W+", query.strip()) if t]
        results: list[SavedFile] = []
        for legacy in self.files:
            full = f"/{legacy}"
            meta = self.metadata.get(full) or SavedFile(
                path=full,
                name=legacy.rsplit("/", 1)[-1],
                web_view_link=f"https://mock-storage.example.com{full}",
            )
            haystack = " ".join(
                [meta.name.lower(), meta.path.lower(), (meta.description or "").lower()]
            )
            if tokens and not all(t in haystack for t in tokens):
                continue
            results.append(meta)
        results.sort(key=lambda f: f.modified_at or "", reverse=True)
        return results[:limit]
