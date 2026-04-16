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

from backend.app.services.companycam_models import Photo, Project, User

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

    async def search_projects(self, query: str) -> list[Project]:
        """Search CompanyCam projects by name or address."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects",
                params={"query": query},
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

    async def list_project_photos(self, project_id: str) -> list[Photo]:
        """List photos in a CompanyCam project."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/projects/{project_id}/photos",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return [Photo.model_validate(_normalize_photo(p)) for p in resp.json()]


def get_photo_url(photo: Photo) -> str:
    """Extract the best available URL from a CompanyCam photo."""
    if photo.uris:
        for uri_entry in photo.uris:
            if uri_entry.type == "original":
                return uri_entry.uri
        return photo.uris[0].uri
    return f"{_API_BASE}/photos/{photo.id}"
