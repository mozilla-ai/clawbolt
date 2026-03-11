"""QuickBooks Online service abstraction.

Provides an ABC for QuickBooks operations and a concrete implementation
that calls the QBO REST API via httpx.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from backend.app.config import Settings, settings

logger = logging.getLogger(__name__)

QBO_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
QBO_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


class QuickBooksService(ABC):
    """Abstract base for QuickBooks operations."""

    @abstractmethod
    async def list_invoices(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        """Search QBO invoices, optionally filtered by customer name."""

    @abstractmethod
    async def list_estimates(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        """Search QBO estimates, optionally filtered by customer name."""


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
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._realm_id = realm_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        base = QBO_PRODUCTION_BASE if environment == "production" else QBO_SANDBOX_BASE
        self._api_base = f"{base}/v3/company/{realm_id}"
        self._http = httpx.AsyncClient(timeout=30.0)

    async def _refresh_access_token(self) -> None:
        """Refresh the OAuth2 access token using the refresh token."""
        logger.info("Refreshing QuickBooks access token")
        resp = await self._http.post(
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

        resp = await self._http.request(method, url, headers=headers, json=json, params=params)

        if resp.status_code == 401:
            await self._refresh_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
            resp = await self._http.request(method, url, headers=headers, json=json, params=params)

        resp.raise_for_status()
        return resp.json()

    async def _query(self, query_str: str) -> list[dict[str, Any]]:
        """Run a QBO query and return the list of results."""
        data = await self._request("GET", "/query", params={"query": query_str})
        response = data.get("QueryResponse", {})
        # QBO returns results under the entity name key; grab the first list found
        for value in response.values():
            if isinstance(value, list):
                return value
        return []

    async def list_invoices(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        if customer_name:
            escaped = customer_name.replace("'", "\\'")
            qs = f"SELECT * FROM Invoice WHERE CustomerRef IN (SELECT Id FROM Customer WHERE DisplayName LIKE '%{escaped}%') MAXRESULTS 50"
        else:
            qs = "SELECT * FROM Invoice MAXRESULTS 50"
        raw = await self._query(qs)
        return [
            {
                "id": inv["Id"],
                "doc_number": inv.get("DocNumber", ""),
                "customer_name": (inv.get("CustomerRef") or {}).get("name", ""),
                "total": inv.get("TotalAmt", 0),
                "balance": inv.get("Balance", 0),
                "due_date": inv.get("DueDate", ""),
                "date": inv.get("TxnDate", ""),
                "email_status": inv.get("EmailStatus", ""),
            }
            for inv in raw
        ]

    async def list_estimates(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        if customer_name:
            escaped = customer_name.replace("'", "\\'")
            qs = f"SELECT * FROM Estimate WHERE CustomerRef IN (SELECT Id FROM Customer WHERE DisplayName LIKE '%{escaped}%') MAXRESULTS 50"
        else:
            qs = "SELECT * FROM Estimate MAXRESULTS 50"
        raw = await self._query(qs)
        return [
            {
                "id": est["Id"],
                "doc_number": est.get("DocNumber", ""),
                "customer_name": (est.get("CustomerRef") or {}).get("name", ""),
                "total": est.get("TotalAmt", 0),
                "date": est.get("TxnDate", ""),
                "expiry_date": est.get("ExpirationDate", ""),
                "status": est.get("TxnStatus", ""),
            }
            for est in raw
        ]


def get_quickbooks_service(
    svc_settings: Settings | None = None,
) -> QuickBooksService | None:
    """Factory: return the configured QuickBooks service, or None when not configured."""
    s = svc_settings or settings
    if not s.quickbooks_client_id or not s.quickbooks_client_secret:
        return None
    return QuickBooksOnlineService(
        client_id=s.quickbooks_client_id,
        client_secret=s.quickbooks_client_secret,
        realm_id=s.quickbooks_realm_id,
        access_token=s.quickbooks_access_token,
        refresh_token=s.quickbooks_refresh_token,
        environment=s.quickbooks_environment,
    )
