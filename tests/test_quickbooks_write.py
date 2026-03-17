"""Tests for QuickBooks write operations (create, send, convert)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.agent.tools.quickbooks_tools import create_quickbooks_tools
from backend.app.services.quickbooks_service import QuickBooksService


class FakeQBService(QuickBooksService):
    """In-memory fake for testing QB write tools."""

    def __init__(self) -> None:
        self.customers: dict[str, dict[str, Any]] = {
            "1": {"Id": "1", "DisplayName": "Alice Johnson"},
            "2": {"Id": "2", "DisplayName": "Bob's Plumbing"},
        }
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.sent_invoices: list[tuple[str, str]] = []
        self._next_id = 100

    async def query(self, query_str: str) -> list[dict[str, Any]]:
        upper = query_str.upper()
        if "FROM CUSTOMER" in upper:
            # Simple name match
            for cust in self.customers.values():
                if cust["DisplayName"] in query_str:
                    return [cust]
            return []
        if "FROM ESTIMATE" in upper:
            # Return a fake estimate if id matches
            for entity_type, data in self.created:
                if entity_type == "Estimate" and str(data.get("_Id", "")) in query_str:
                    return [
                        {
                            "Id": str(data["_Id"]),
                            "CustomerRef": data.get("CustomerRef", {}),
                            "Line": data.get("Line", []),
                            "TotalAmt": 500.0,
                        }
                    ]
            return []
        return []

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        result = {
            "Id": str(self._next_id),
            "DocNumber": f"10{self._next_id}",
            "TotalAmt": sum(line.get("Amount", 0) for line in data.get("Line", [])),
            **data,
        }
        data["_Id"] = self._next_id
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
# qb_create_estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_estimate_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_estimate")

    result = await fn(
        customer_name="Alice Johnson",
        line_items=[
            {"description": "Labor", "quantity": 8, "unit_price": 50.0},
            {"description": "Materials", "quantity": 1, "unit_price": 200.0},
        ],
        expiration_date="2026-04-01",
        memo="Kitchen remodel estimate",
    )

    assert result.is_error is False
    assert "Estimate created" in result.content
    assert "Alice Johnson" in result.content
    assert len(svc.created) == 1
    entity_type, body = svc.created[0]
    assert entity_type == "Estimate"
    assert body["CustomerRef"]["value"] == "1"
    assert body["ExpirationDate"] == "2026-04-01"
    assert body["CustomerMemo"]["value"] == "Kitchen remodel estimate"


@pytest.mark.asyncio()
async def test_qb_create_estimate_customer_not_found() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_estimate")

    result = await fn(
        customer_name="Nonexistent Customer",
        line_items=[{"description": "Test", "quantity": 1, "unit_price": 100.0}],
    )

    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_qb_create_estimate_api_error() -> None:
    svc = FakeQBService()
    svc.create_entity = AsyncMock(side_effect=Exception("QB API error"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_estimate")

    result = await fn(
        customer_name="Alice Johnson",
        line_items=[{"description": "Test", "quantity": 1, "unit_price": 100.0}],
    )

    assert result.is_error is True
    assert "Failed to create estimate" in result.content


# ---------------------------------------------------------------------------
# qb_create_invoice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_invoice_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_invoice")

    result = await fn(
        customer_name="Alice Johnson",
        line_items=[
            {"description": "Pipe repair", "quantity": 1, "unit_price": 350.0},
        ],
        due_date="2026-04-15",
    )

    assert result.is_error is False
    assert "Invoice created" in result.content
    assert "Alice Johnson" in result.content
    entity_type, body = svc.created[0]
    assert entity_type == "Invoice"
    assert body["DueDate"] == "2026-04-15"


@pytest.mark.asyncio()
async def test_qb_create_invoice_customer_not_found() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_invoice")

    result = await fn(
        customer_name="Nobody",
        line_items=[{"description": "Test", "quantity": 1, "unit_price": 100.0}],
    )

    assert result.is_error is True
    assert "not found" in result.content.lower()


# ---------------------------------------------------------------------------
# qb_create_customer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_customer_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_customer")

    result = await fn(
        display_name="New Customer LLC",
        email="new@example.com",
        phone="555-1234",
    )

    assert result.is_error is False
    assert "Customer created" in result.content
    assert "New Customer LLC" in result.content
    entity_type, body = svc.created[0]
    assert entity_type == "Customer"
    assert body["DisplayName"] == "New Customer LLC"
    assert body["PrimaryEmailAddr"]["Address"] == "new@example.com"
    assert body["PrimaryPhone"]["FreeFormNumber"] == "555-1234"


@pytest.mark.asyncio()
async def test_qb_create_customer_minimal() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_customer")

    result = await fn(display_name="Just A Name")

    assert result.is_error is False
    _, body = svc.created[0]
    assert "PrimaryEmailAddr" not in body
    assert "PrimaryPhone" not in body


@pytest.mark.asyncio()
async def test_qb_create_customer_api_error() -> None:
    svc = FakeQBService()
    svc.create_entity = AsyncMock(side_effect=Exception("Duplicate name"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create_customer")

    result = await fn(display_name="Duplicate")

    assert result.is_error is True
    assert "Failed to create customer" in result.content


# ---------------------------------------------------------------------------
# qb_send_invoice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_send_invoice_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send_invoice")

    result = await fn(invoice_id="42", email="client@example.com")

    assert result.is_error is False
    assert "sent to client@example.com" in result.content
    assert svc.sent_invoices == [("42", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_invoice_failure() -> None:
    svc = FakeQBService()
    svc.send_invoice_email = AsyncMock(side_effect=Exception("Email failed"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send_invoice")

    result = await fn(invoice_id="42", email="bad@email")

    assert result.is_error is True
    assert "Failed to send invoice" in result.content


# ---------------------------------------------------------------------------
# qb_estimate_to_invoice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_estimate_to_invoice_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)

    # First create an estimate
    create_est = _get_tool(tools, "qb_create_estimate")
    await create_est(
        customer_name="Alice Johnson",
        line_items=[{"description": "Deck build", "quantity": 1, "unit_price": 5000.0}],
    )

    # Get the estimate ID from the created entities
    _, est_data = svc.created[0]
    est_id = str(est_data["_Id"])

    # Convert to invoice
    convert = _get_tool(tools, "qb_estimate_to_invoice")
    result = await convert(estimate_id=est_id)

    assert result.is_error is False
    assert "Invoice created from Estimate" in result.content

    # Verify the invoice was created with LinkedTxn
    inv_entity_type, inv_body = svc.created[1]
    assert inv_entity_type == "Invoice"
    assert inv_body["LinkedTxn"][0]["TxnId"] == est_id
    assert inv_body["LinkedTxn"][0]["TxnType"] == "Estimate"


@pytest.mark.asyncio()
async def test_qb_estimate_to_invoice_not_found() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_estimate_to_invoice")

    result = await fn(estimate_id="99999")

    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_qb_estimate_to_invoice_create_fails() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)

    # Create an estimate first
    create_est = _get_tool(tools, "qb_create_estimate")
    await create_est(
        customer_name="Alice Johnson",
        line_items=[{"description": "Test", "quantity": 1, "unit_price": 100.0}],
    )
    _, est_data = svc.created[0]
    est_id = str(est_data["_Id"])

    # Make create_entity fail for the invoice
    original = svc.create_entity
    call_count = 0

    async def fail_second(entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if entity_type == "Invoice":
            raise Exception("QB error")
        return await original(entity_type, data)

    svc.create_entity = fail_second  # type: ignore[assignment]

    convert = _get_tool(tools, "qb_estimate_to_invoice")
    result = await convert(estimate_id=est_id)

    assert result.is_error is True
    assert "Failed to create invoice from estimate" in result.content


# ---------------------------------------------------------------------------
# Tool count and names
# ---------------------------------------------------------------------------


def test_quickbooks_tools_count() -> None:
    """create_quickbooks_tools should return 6 tools."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    assert len(tools) == 6

    names = {t.name for t in tools}
    assert names == {
        "qb_query",
        "qb_create_estimate",
        "qb_create_invoice",
        "qb_create_customer",
        "qb_send_invoice",
        "qb_estimate_to_invoice",
    }
