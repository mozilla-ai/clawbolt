"""QuickBooks Online service abstraction.

Provides an ABC for QuickBooks operations and a concrete implementation
that calls the QBO REST API via httpx.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

QBO_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
QBO_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


class QuickBooksService(ABC):
    """Abstract base for QuickBooks operations."""

    @abstractmethod
    async def query(self, query_str: str) -> list[dict[str, Any]]:
        """Run a QBO query and return the list of result dicts."""

    @abstractmethod
    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create a QBO entity (Customer, Estimate, Invoice, etc.)."""

    @abstractmethod
    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Update an existing QBO entity. *data* must include Id and SyncToken."""

    @abstractmethod
    async def send_entity_email(
        self, entity_type: str, entity_id: str, email: str
    ) -> dict[str, Any]:
        """Send an invoice or estimate via QuickBooks email."""


class QuickBooksOnlineService(QuickBooksService):
    """Concrete implementation calling the QBO REST API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        realm_id: str,
        access_token: str,
        refresh_token: str,
        environment: str = "sandbox",
        on_token_refresh: Callable[[str, str], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._realm_id = realm_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        base = QBO_PRODUCTION_BASE if environment == "production" else QBO_SANDBOX_BASE
        self._api_base = f"{base}/v3/company/{realm_id}"

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> None:
        """Refresh the OAuth2 access token using the refresh token."""
        logger.info("Refreshing QuickBooks access token")
        resp = await client.post(
            QBO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            auth=(self._client_id, self._client_secret),
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        if self._on_token_refresh:
            self._on_token_refresh(self._access_token, self._refresh_token)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the QBO API with token refresh on 401."""
        url = f"{self._api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, headers=headers, json=json, params=params)

            if resp.status_code == 401:
                await self._refresh_access_token(client)
                headers["Authorization"] = f"Bearer {self._access_token}"
                resp = await client.request(method, url, headers=headers, json=json, params=params)

            resp.raise_for_status()
            return resp.json()

    async def query(self, query_str: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/query", params={"query": query_str})
        response = data.get("QueryResponse", {})
        # QBO returns results under the entity name key; grab the first list found
        for value in response.values():
            if isinstance(value, list):
                return value
        return []

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        path = f"/{entity_type.lower()}"
        result = await self._request("POST", path, json=data)
        # QBO wraps the created entity under the entity type key
        return result.get(entity_type, result)

    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        # QBO uses the same POST endpoint for create and update.
        # The presence of Id + SyncToken in the payload triggers an update.
        path = f"/{entity_type.lower()}"
        result = await self._request("POST", path, json=data)
        return result.get(entity_type, result)

    async def send_entity_email(
        self, entity_type: str, entity_id: str, email: str
    ) -> dict[str, Any]:
        if not entity_id.strip().isdigit():
            msg = f"Invalid entity_id '{entity_id}'. QuickBooks IDs must be numeric."
            raise ValueError(msg)
        import re as _re

        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            msg = f"Invalid email address: '{email}'"
            raise ValueError(msg)
        return await self._request(
            "POST",
            f"/{entity_type.lower()}/{entity_id.strip()}/send",
            params={"sendTo": email},
        )
