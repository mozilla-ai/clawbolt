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
    async def list_items(self, query: str | None = None) -> list[dict[str, Any]]:
        """Search QBO items/services for pricing. Returns list of item dicts."""

    @abstractmethod
    async def list_customers(self, query: str | None = None) -> list[dict[str, Any]]:
        """Search QBO customers. Returns list of customer dicts."""

    @abstractmethod
    async def create_invoice(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an invoice in QBO. Returns the created invoice dict."""

    @abstractmethod
    async def send_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Email an invoice to the customer via QBO. Returns send result dict."""

    @abstractmethod
    async def create_estimate(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an estimate in QBO. Returns the created estimate dict."""


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

    async def list_items(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            escaped = query.replace("'", "\\'")
            qs = f"SELECT * FROM Item WHERE Name LIKE '%{escaped}%'"
        else:
            qs = "SELECT * FROM Item MAXRESULTS 100"
        raw = await self._query(qs)
        return [
            {
                "id": item["Id"],
                "name": item.get("Name", ""),
                "description": item.get("Description", ""),
                "unit_price": item.get("UnitPrice", 0),
                "type": item.get("Type", ""),
            }
            for item in raw
        ]

    async def list_customers(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            escaped = query.replace("'", "\\'")
            qs = f"SELECT * FROM Customer WHERE DisplayName LIKE '%{escaped}%'"
        else:
            qs = "SELECT * FROM Customer MAXRESULTS 100"
        raw = await self._query(qs)
        return [
            {
                "id": item["Id"],
                "display_name": item.get("DisplayName", ""),
                "primary_email": (item.get("PrimaryEmailAddr") or {}).get("Address", ""),
                "primary_phone": (item.get("PrimaryPhone") or {}).get("FreeFormNumber", ""),
                "balance": item.get("Balance", 0),
            }
            for item in raw
        ]

    async def create_invoice(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        qbo_lines = []
        for i, item in enumerate(line_items, start=1):
            line: dict[str, Any] = {
                "LineNum": i,
                "Amount": float(item.get("amount", 0)),
                "DetailType": "SalesItemLineDetail",
                "Description": item.get("description", ""),
                "SalesItemLineDetail": {
                    "Qty": float(item.get("quantity", 1)),
                    "UnitPrice": float(item.get("unit_price", 0)),
                },
            }
            if item.get("item_id"):
                line["SalesItemLineDetail"]["ItemRef"] = {"value": item["item_id"]}
            qbo_lines.append(line)

        body = {
            "CustomerRef": {"value": customer_id},
            "Line": qbo_lines,
        }

        data = await self._request("POST", "/invoice", json=body)
        invoice = data.get("Invoice", data)
        return {
            "id": invoice.get("Id", ""),
            "doc_number": invoice.get("DocNumber", ""),
            "total": invoice.get("TotalAmt", 0),
            "balance": invoice.get("Balance", 0),
            "status": "created",
        }

    async def send_invoice(self, invoice_id: str) -> dict[str, Any]:
        data = await self._request("POST", f"/invoice/{invoice_id}/send")
        invoice = data.get("Invoice", data)
        return {
            "id": invoice.get("Id", ""),
            "email_status": invoice.get("EmailStatus", ""),
            "status": "sent",
        }

    async def create_estimate(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        qbo_lines = []
        for i, item in enumerate(line_items, start=1):
            line: dict[str, Any] = {
                "LineNum": i,
                "Amount": float(item.get("amount", 0)),
                "DetailType": "SalesItemLineDetail",
                "Description": item.get("description", ""),
                "SalesItemLineDetail": {
                    "Qty": float(item.get("quantity", 1)),
                    "UnitPrice": float(item.get("unit_price", 0)),
                },
            }
            if item.get("item_id"):
                line["SalesItemLineDetail"]["ItemRef"] = {"value": item["item_id"]}
            qbo_lines.append(line)

        body = {
            "CustomerRef": {"value": customer_id},
            "Line": qbo_lines,
        }

        data = await self._request("POST", "/estimate", json=body)
        estimate = data.get("Estimate", data)
        return {
            "id": estimate.get("Id", ""),
            "doc_number": estimate.get("DocNumber", ""),
            "total": estimate.get("TotalAmt", 0),
            "status": "created",
        }


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
