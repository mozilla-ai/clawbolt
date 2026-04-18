"""CompanyCam API service.

Provides methods for interacting with the CompanyCam REST API v2:
searching/creating projects, uploading photos, and listing project photos.

All return types use Pydantic models generated from CompanyCam's OpenAPI spec
(see companycam_models.py).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.app.services.companycam_models import (
    Checklist,
    ChecklistTemplate,
    Comment,
    Document,
    Photo,
    Project,
    Tag,
    User,
)

logger = logging.getLogger(__name__)

_API_BASE = "https://api.companycam.com/v2"


def _normalize_photo(data: dict[str, Any]) -> dict[str, Any]:
    """Fix known mismatches between CompanyCam's OpenAPI spec and actual API.

    - coordinates: spec says list[Coordinate], API returns a single dict
    - description: spec says str, API returns a dict with id/html/text fields
    """
    if isinstance(data.get("coordinates"), dict):
        data["coordinates"] = [data["coordinates"]]
    desc = data.get("description")
    if isinstance(desc, dict):
        data["description"] = desc.get("text", desc.get("html", str(desc)))
    return data


class CompanyCamService:
    """Client for the CompanyCam REST API v2.

    Requires a Bearer access token (API token or OAuth token).
    """

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("CompanyCam access token is required")
        self._access_token = access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    async def validate_token(self) -> User:
        """Validate the access token by fetching the current user.

        Returns the user profile on success. Raises on auth failure.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_API_BASE}/users/current",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return User.model_validate(resp.json())

    async def search_projects(
        self,
        query: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Project]:
        """Search CompanyCam projects by name or address."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects",
                params={"query": query, "page": page, "per_page": per_page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Project.model_validate(p) for p in resp.json()]

    async def create_project(self, name: str, address: str = "") -> Project:
        """Create a new CompanyCam project."""
        body: dict[str, object] = {"name": name}
        if address:
            body["address"] = {"street_address_1": address}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/projects",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Project.model_validate(resp.json())

    async def upload_photo(
        self,
        project_id: str,
        photo_uri: str,
        tags: list[str] | None = None,
        description: str = "",
    ) -> Photo:
        """Upload a photo to a CompanyCam project.

        The CompanyCam API requires a publicly accessible ``photo_uri``
        that their servers download. Use the temp media endpoint to serve
        staged bytes when no permanent URL is available.
        """
        logger.info(
            "Uploading to CompanyCam: project=%s uri=%s",
            project_id,
            photo_uri,
        )

        photo_body: dict[str, object] = {
            "uri": photo_uri,
            "captured_at": int(time.time()),
        }
        if tags:
            photo_body["tags"] = tags
        if description:
            photo_body["description"] = description

        headers = {**self._headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{_API_BASE}/projects/{project_id}/photos",
                json={"photo": photo_body},
                headers=headers,
            )
            resp.raise_for_status()
            raw = resp.json()
            logger.info(
                "CompanyCam photo response: id=%s status=%s hash=%s uri_count=%s",
                raw.get("id"),
                raw.get("processing_status"),
                raw.get("hash"),
                len(raw.get("uris", [])),
            )
            if raw.get("processing_status") in ("processing_error", "duplicate"):
                logger.warning(
                    "CompanyCam photo may not appear: status=%s (id=%s)",
                    raw.get("processing_status"),
                    raw.get("id"),
                )
            return Photo.model_validate(_normalize_photo(raw))

    async def get_photo(self, photo_id: str) -> Photo:
        """Fetch a single photo by ID."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{_API_BASE}/photos/{photo_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Photo.model_validate(_normalize_photo(resp.json()))

    async def update_project(
        self,
        project_id: str,
        name: str | None = None,
        address: str | None = None,
    ) -> Project:
        """Update an existing CompanyCam project."""
        body: dict[str, object] = {}
        if name is not None:
            body["name"] = name
        if address is not None:
            body["address"] = {"street_address_1": address}
        if not body:
            raise ValueError("At least one field (name or address) must be provided")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                f"{_API_BASE}/projects/{project_id}",
                json=body,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Project.model_validate(resp.json())

    async def list_project_photos(
        self,
        project_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Photo]:
        """List photos in a CompanyCam project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/photos",
                params={"page": page, "per_page": per_page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Photo.model_validate(_normalize_photo(p)) for p in resp.json()]

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    async def get_project(self, project_id: str) -> Project:
        """Fetch full details for a single project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Project.model_validate(resp.json())

    async def delete_project(self, project_id: str) -> None:
        """Permanently delete a CompanyCam project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{_API_BASE}/projects/{project_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()

    async def archive_project(self, project_id: str) -> None:
        """Archive a CompanyCam project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(
                f"{_API_BASE}/projects/{project_id}/archive",
                headers=self._headers(),
            )
            resp.raise_for_status()

    async def restore_project(self, project_id: str) -> None:
        """Restore an archived CompanyCam project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                f"{_API_BASE}/projects/{project_id}/restore",
                headers=self._headers(),
            )
            resp.raise_for_status()

    async def update_notepad(self, project_id: str, notepad: str) -> None:
        """Update the notepad (free-text notes) on a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                f"{_API_BASE}/projects/{project_id}/notepad",
                json={"notepad": notepad},
                headers=self._headers(),
            )
            resp.raise_for_status()

    # ------------------------------------------------------------------
    # Project content
    # ------------------------------------------------------------------

    async def list_project_documents(
        self,
        project_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Document]:
        """List documents attached to a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/documents",
                params={"page": page, "per_page": per_page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Document.model_validate(d) for d in resp.json()]

    async def list_project_comments(
        self,
        project_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Comment]:
        """List comments on a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/comments",
                params={"page": page, "per_page": per_page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Comment.model_validate(c) for c in resp.json()]

    async def add_project_comment(self, project_id: str, content: str) -> Comment:
        """Add a comment to a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/projects/{project_id}/comments",
                json={"comment": {"content": content}},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Comment.model_validate(resp.json())

    async def list_project_labels(self, project_id: str) -> list[Tag]:
        """List labels/tags assigned to a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/labels",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Tag.model_validate(t) for t in resp.json()]

    async def add_project_labels(self, project_id: str, labels: list[str]) -> list[Tag]:
        """Add labels to a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/projects/{project_id}/labels",
                json={"project": {"labels": labels}},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return [Tag.model_validate(t) for t in data]
            return [Tag.model_validate(data)]

    # ------------------------------------------------------------------
    # Photo management
    # ------------------------------------------------------------------

    async def search_photos(
        self,
        project_id: str | None = None,
        start_date: int | None = None,
        end_date: int | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Photo]:
        """Search photos across all projects with optional filters."""
        params: dict[str, str | int] = {"page": page, "per_page": per_page}
        if project_id:
            params["project_id"] = project_id
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/photos",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Photo.model_validate(_normalize_photo(p)) for p in resp.json()]

    async def delete_photo(self, photo_id: str) -> None:
        """Permanently delete a photo."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{_API_BASE}/photos/{photo_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()

    async def list_photo_tags(self, photo_id: str) -> list[Tag]:
        """List tags on a photo."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/photos/{photo_id}/tags",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Tag.model_validate(t) for t in resp.json()]

    async def add_photo_tags(self, photo_id: str, tags: list[str]) -> list[Tag]:
        """Add tags to a photo."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/photos/{photo_id}/tags",
                json={"tags": tags},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return [Tag.model_validate(t) for t in data]
            return [Tag.model_validate(data)]

    async def list_photo_comments(
        self,
        photo_id: str,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Comment]:
        """List comments on a photo."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/photos/{photo_id}/comments",
                params={"page": page, "per_page": per_page},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Comment.model_validate(c) for c in resp.json()]

    async def add_photo_comment(self, photo_id: str, content: str) -> Comment:
        """Add a comment to a photo."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/photos/{photo_id}/comments",
                json={"comment": {"content": content}},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Comment.model_validate(resp.json())

    # ------------------------------------------------------------------
    # Checklists
    # ------------------------------------------------------------------

    async def list_checklist_templates(self) -> list[ChecklistTemplate]:
        """List available checklist templates."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/checklists",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [ChecklistTemplate.model_validate(t) for t in resp.json()]

    async def list_project_checklists(self, project_id: str) -> list[Checklist]:
        """List checklists attached to a project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/checklists",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Checklist.model_validate(c) for c in resp.json()]

    async def create_project_checklist(
        self,
        project_id: str,
        template_id: str,
    ) -> Checklist:
        """Create a checklist on a project from a template."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_API_BASE}/projects/{project_id}/checklists",
                json={"checklist_template_id": template_id},
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Checklist.model_validate(resp.json())

    async def get_checklist(
        self,
        project_id: str,
        checklist_id: str,
    ) -> Checklist:
        """Get a checklist with full task details."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/checklists/{checklist_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return Checklist.model_validate(resp.json())


def get_photo_url(photo: Photo) -> str:
    """Extract the best available URL from a CompanyCam photo."""
    if photo.uris:
        for uri_entry in photo.uris:
            if uri_entry.type == "original" and uri_entry.uri:
                return uri_entry.uri
        for uri_entry in photo.uris:
            if uri_entry.uri:
                return uri_entry.uri
    return f"{_API_BASE}/photos/{photo.id}"
