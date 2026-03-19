"""Tests for QuickBooks write operations (qb_create, qb_send)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.agent.tools.quickbooks_tools import create_quickbooks_tools
from backend.app.services.quickbooks_service import QuickBooksService


class FakeQBService(QuickBooksService):
    """In-memory fake for testing QB write tools."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.sent_invoices: list[tuple[str, str]] = []
        self._next_id = 100

    async def query(self, query_str: str) -> list[dict[str, Any]]:
        return []

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        result: dict[str, Any] = {
            "Id": str(self._next_id),
            **data,
        }
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        else:
            result["DocNumber"] = f"10{self._next_id}"
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        self.created.append((entity_type, data))
        return result

    async def send_invoice_email(self, invoice_id: str, email: str) -> dict[str, Any]:
        self.sent_invoices.append((invoice_id, email))
        return {"Invoice": {"Id": invoice_id, "EmailStatus": "EmailSent"}}


def _get_tool(tools: list, name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t.function
    raise KeyError(f"Tool {name} not found")


# ---------------------------------------------------------------------------
# qb_create - Customer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_customer() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Customer",
        data={
            "DisplayName": "New Customer LLC",
            "PrimaryEmailAddr": {"Address": "new@example.com"},
            "PrimaryPhone": {"FreeFormNumber": "555-1234"},
        },
    )

    assert result.is_error is False
    assert "Customer created" in result.content
    assert "New Customer LLC" in result.content
    assert len(svc.created) == 1
    entity_type, body = svc.created[0]
    assert entity_type == "Customer"
    assert body["DisplayName"] == "New Customer LLC"
    assert body["PrimaryEmailAddr"]["Address"] == "new@example.com"


@pytest.mark.asyncio()
async def test_qb_create_customer_minimal() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Customer",
        data={"DisplayName": "Just A Name"},
    )

    assert result.is_error is False
    _, body = svc.created[0]
    assert "PrimaryEmailAddr" not in body
    assert "PrimaryPhone" not in body


# ---------------------------------------------------------------------------
# qb_create - Estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_estimate() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Estimate",
        data={
            "CustomerRef": {"value": "1"},
            "Line": [
                {
                    "Amount": 400.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Labor",
                    "SalesItemLineDetail": {"Qty": 8, "UnitPrice": 50.0},
                },
                {
                    "Amount": 200.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Materials",
                    "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 200.0},
                },
            ],
            "ExpirationDate": "2026-04-01",
            "CustomerMemo": {"value": "Kitchen remodel estimate"},
        },
    )

    assert result.is_error is False
    assert "Estimate created" in result.content
    assert "$600.00" in result.content
    assert len(svc.created) == 1
    entity_type, body = svc.created[0]
    assert entity_type == "Estimate"
    assert body["CustomerRef"]["value"] == "1"
    assert body["ExpirationDate"] == "2026-04-01"


# ---------------------------------------------------------------------------
# qb_create - Invoice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_invoice() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Invoice",
        data={
            "CustomerRef": {"value": "1"},
            "Line": [
                {
                    "Amount": 350.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Pipe repair",
                    "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 350.0},
                },
            ],
            "DueDate": "2026-04-15",
        },
    )

    assert result.is_error is False
    assert "Invoice created" in result.content
    assert "$350.00" in result.content
    entity_type, body = svc.created[0]
    assert entity_type == "Invoice"
    assert body["DueDate"] == "2026-04-15"


@pytest.mark.asyncio()
async def test_qb_create_invoice_with_linked_estimate() -> None:
    """Creating an invoice with LinkedTxn (estimate-to-invoice workflow)."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Invoice",
        data={
            "CustomerRef": {"value": "1"},
            "Line": [
                {
                    "Amount": 5000.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Deck build",
                    "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 5000.0},
                },
            ],
            "LinkedTxn": [{"TxnId": "42", "TxnType": "Estimate"}],
        },
    )

    assert result.is_error is False
    assert "Invoice created" in result.content
    _, body = svc.created[0]
    assert body["LinkedTxn"][0]["TxnId"] == "42"
    assert body["LinkedTxn"][0]["TxnType"] == "Estimate"


# ---------------------------------------------------------------------------
# qb_create - validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_rejects_disallowed_entity() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(entity_type="Payment", data={"TotalAmt": 100})

    assert result.is_error is True
    assert "not allowed" in result.content


@pytest.mark.asyncio()
async def test_qb_create_api_error() -> None:
    svc = FakeQBService()
    svc.create_entity = AsyncMock(side_effect=Exception("QB API error"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Customer",
        data={"DisplayName": "Test"},
    )

    assert result.is_error is True
    assert "Failed to create Customer" in result.content


# ---------------------------------------------------------------------------
# qb_send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_send_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(invoice_id="42", email="client@example.com")

    assert result.is_error is False
    assert "sent to client@example.com" in result.content
    assert svc.sent_invoices == [("42", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_failure() -> None:
    svc = FakeQBService()
    svc.send_invoice_email = AsyncMock(side_effect=Exception("Email failed"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(invoice_id="42", email="bad@email.com")

    assert result.is_error is True
    assert "Failed to send invoice" in result.content


# ---------------------------------------------------------------------------
# Tool count and names
# ---------------------------------------------------------------------------


def test_quickbooks_tools_count() -> None:
    """create_quickbooks_tools should return 3 tools."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    assert len(tools) == 3

    names = {t.name for t in tools}
    assert names == {"qb_query", "qb_create", "qb_send"}
