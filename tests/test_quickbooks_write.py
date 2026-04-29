"""Tests for QuickBooks write operations (qb_create, qb_send)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from backend.app.integrations.quickbooks.factory import (
    QBCreateParams,
    QBUpdateParams,
    create_quickbooks_tools,
)
from backend.app.integrations.quickbooks.service import QuickBooksOnlineService, QuickBooksService


class FakeQBService(QuickBooksService):
    """In-memory fake for testing QB write tools."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[tuple[str, str, str]] = []
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

    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {**data}
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        else:
            result["DocNumber"] = data.get("DocNumber", "")
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        self.updated.append((entity_type, data))
        return result

    async def send_entity_email(
        self, entity_type: str, entity_id: str, email: str
    ) -> dict[str, Any]:
        self.sent.append((entity_type, entity_id, email))
        return {entity_type: {"Id": entity_id, "EmailStatus": "EmailSent"}}


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
# qb_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_update_estimate() -> None:
    """Update an estimate with changed line items."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_update")

    result = await fn(
        entity_type="Estimate",
        data={
            "Id": "2001",
            "SyncToken": "0",
            "CustomerRef": {"value": "100"},
            "Line": [
                {
                    "Amount": 600.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Labor - updated",
                    "SalesItemLineDetail": {"Qty": 12, "UnitPrice": 50.0},
                },
            ],
        },
    )

    assert result.is_error is False
    assert "Estimate updated" in result.content
    assert "Id: 2001" in result.content
    assert "$600.00" in result.content
    assert len(svc.updated) == 1
    entity_type, body = svc.updated[0]
    assert entity_type == "Estimate"
    assert body["Id"] == "2001"
    assert body["SyncToken"] == "0"


@pytest.mark.asyncio()
async def test_qb_update_customer() -> None:
    """Update a customer's contact info."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_update")

    result = await fn(
        entity_type="Customer",
        data={
            "Id": "100",
            "SyncToken": "1",
            "DisplayName": "John Smith",
            "PrimaryPhone": {"FreeFormNumber": "555-9999"},
        },
    )

    assert result.is_error is False
    assert "Customer updated" in result.content
    assert "John Smith" in result.content
    _, body = svc.updated[0]
    assert body["PrimaryPhone"]["FreeFormNumber"] == "555-9999"


@pytest.mark.asyncio()
async def test_qb_update_rejects_disallowed_entity() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_update")

    result = await fn(
        entity_type="Payment",
        data={"Id": "1", "SyncToken": "0", "TotalAmt": 100},
    )

    assert result.is_error is True
    assert "not allowed" in result.content


# ---------------------------------------------------------------------------
# data param coercion: accept JSON-encoded strings as well as dicts
# ---------------------------------------------------------------------------


def test_qb_create_params_accepts_json_string_data() -> None:
    """The LLM occasionally passes `data` as a JSON-encoded string instead
    of a dict. The params model should parse it transparently so the call
    succeeds on the first round."""
    payload = {
        "CustomerRef": {"value": "3"},
        "Line": [
            {
                "Amount": 100.0,
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 100.0},
            }
        ],
    }
    encoded = json.dumps(payload)

    from_string = QBCreateParams.model_validate({"entity_type": "Estimate", "data": encoded})
    from_dict = QBCreateParams.model_validate({"entity_type": "Estimate", "data": payload})

    assert from_string.data == payload
    assert from_string.data == from_dict.data


def test_qb_update_params_accepts_json_string_data() -> None:
    payload = {
        "Id": "3",
        "SyncToken": "2",
        "CustomerRef": {"value": "100"},
    }
    encoded = json.dumps(payload)

    parsed = QBUpdateParams.model_validate({"entity_type": "Estimate", "data": encoded})

    assert parsed.data == payload


def test_qb_create_params_unparseable_string_raises_validation_error() -> None:
    """An unparseable string must surface as a Pydantic ValidationError so
    the agent can hand a structured retry hint back to the LLM, rather than
    blowing up later as a ServerError inside the tool function."""
    with pytest.raises(ValidationError) as exc_info:
        QBCreateParams.model_validate({"entity_type": "Customer", "data": "{not valid json"})

    assert "data" in str(exc_info.value)


def test_qb_create_params_json_string_of_non_object_raises() -> None:
    """A JSON-encoded list or scalar is still not a valid QBO payload."""
    with pytest.raises(ValidationError):
        QBCreateParams.model_validate({"entity_type": "Customer", "data": json.dumps([1, 2, 3])})


@pytest.mark.asyncio()
async def test_qb_update_api_error() -> None:
    svc = FakeQBService()
    svc.update_entity = AsyncMock(side_effect=Exception("QB API error"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_update")

    result = await fn(
        entity_type="Estimate",
        data={"Id": "2001", "SyncToken": "0"},
    )

    assert result.is_error is True
    assert "Failed to update Estimate" in result.content


# ---------------------------------------------------------------------------
# qb_send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_send_invoice_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Invoice", entity_id="42", email="client@example.com")

    assert result.is_error is False
    assert "Invoice 42 sent to client@example.com" in result.content
    assert svc.sent == [("Invoice", "42", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_estimate_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Estimate", entity_id="2001", email="client@example.com")

    assert result.is_error is False
    assert "Estimate 2001 sent to client@example.com" in result.content
    assert svc.sent == [("Estimate", "2001", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_rejects_disallowed_entity() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Customer", entity_id="100", email="test@example.com")

    assert result.is_error is True
    assert "not allowed" in result.content


@pytest.mark.asyncio()
async def test_qb_send_failure() -> None:
    svc = FakeQBService()
    svc.send_entity_email = AsyncMock(side_effect=Exception("Email failed"))  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Invoice", entity_id="42", email="bad@email.com")

    assert result.is_error is True
    assert "Failed to send invoice" in result.content


@pytest.mark.asyncio()
async def test_qb_send_failure_surfaces_qbo_error_envelope() -> None:
    """When QBO returns a Fault.Error[] envelope on a send failure, qb_send
    must surface that body so users see Intuit's reason instead of just
    'HTTPStatusError 500'."""
    fault_body = {
        "Fault": {
            "Error": [
                {
                    "Message": "Invalid Email",
                    "Detail": "The supplied email address is not valid.",
                    "code": "2030",
                }
            ],
            "type": "ValidationFault",
        }
    }
    request = httpx.Request("POST", "https://example.invalid/estimate/1/send")
    response = httpx.Response(400, json=fault_body, request=request)
    err = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    svc = FakeQBService()
    svc.send_entity_email = AsyncMock(side_effect=err)  # type: ignore[method-assign]
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Estimate", entity_id="1", email="bad@example.com")

    assert result.is_error is True
    assert "Invalid Email" in result.content
    assert "2030" in result.content


# ---------------------------------------------------------------------------
# Tool count and names
# ---------------------------------------------------------------------------


def test_quickbooks_tools_count() -> None:
    """create_quickbooks_tools should return 4 tools."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    assert len(tools) == 4

    names = {t.name for t in tools}
    assert names == {"qb_query", "qb_create", "qb_update", "qb_send"}


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_invoice_returns_receipt() -> None:
    """Write-side QB tools must populate a ToolReceipt so plain-text
    channels can confirm the mutation without relying on LLM text."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Invoice",
        data={
            "CustomerRef": {"value": "1", "name": "Johnson"},
            "Line": [
                {
                    "Amount": 2560.00,
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Bathroom remodel",
                    "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 2560.0},
                }
            ],
        },
    )

    assert result.receipt is not None
    assert result.receipt.action == "Created QuickBooks invoice for"
    assert "Johnson" in result.receipt.target
    assert "$2,560.00" in result.receipt.target


@pytest.mark.asyncio()
async def test_qb_query_does_not_return_a_receipt() -> None:
    """Read-side queries return data which is self-verifying. They must
    not populate a receipt because no external state mutated."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_query")

    result = await fn(query="SELECT * FROM Invoice MAXRESULTS 5")

    assert result.receipt is None


# ---------------------------------------------------------------------------
# Invariant: ToolResult.content must not contain ToolReceipt.url
# ---------------------------------------------------------------------------
#
# Regression for #1069. The receipt is the canonical channel for surfacing
# deep links on plain-text channels. If a tool also embeds the URL in
# ToolResult.content, the LLM sees it and reproduces it in prose, so the
# user receives the same URL twice.
#
# QB receipt URLs are only built when the service is a
# ``QuickBooksOnlineService`` (see ``_build_qbo_url``), so the invariant
# test below uses a fake that subclasses that concrete type.


class FakeQBOServiceWithURL(QuickBooksOnlineService):
    """Fake QBO service that yields realistic receipt URLs for the
    invariant test. Subclasses ``QuickBooksOnlineService`` so
    ``_build_qbo_url`` returns a real deep link."""

    def __init__(self) -> None:
        super().__init__(
            client_id="test",
            client_secret="test",
            realm_id="9999",
            access_token="test",
            refresh_token="test",
            environment="sandbox",
        )
        self._next_id = 2000

    async def query(self, query_str: str) -> list[dict[str, Any]]:
        return []

    async def create_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        result: dict[str, Any] = {"Id": str(self._next_id), **data}
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        else:
            result["DocNumber"] = f"10{self._next_id}"
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        return result

    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {**data}
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        else:
            result["DocNumber"] = data.get("DocNumber", "10000")
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        return result

    async def send_entity_email(
        self, entity_type: str, entity_id: str, email: str
    ) -> dict[str, Any]:
        return {entity_type: {"Id": entity_id, "EmailStatus": "EmailSent"}}


@pytest.mark.asyncio()
async def test_invariant_no_url_duplication_across_qb_tools() -> None:
    """For every QuickBooks tool returning a ToolReceipt with a URL,
    ToolResult.content must not contain that URL. Regression for #1069."""
    svc = FakeQBOServiceWithURL()
    tools = create_quickbooks_tools(svc)
    create_fn = _get_tool(tools, "qb_create")
    update_fn = _get_tool(tools, "qb_update")
    send_fn = _get_tool(tools, "qb_send")

    invoice_data = {
        "CustomerRef": {"value": "1", "name": "Acme Plumbing"},
        "Line": [
            {
                "Amount": 350.00,
                "DetailType": "SalesItemLineDetail",
                "Description": "Pipe repair",
                "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 350.0},
            },
        ],
    }

    results = [
        await create_fn(entity_type="Invoice", data=invoice_data),
        await update_fn(
            entity_type="Invoice",
            data={"Id": "2001", "SyncToken": "0", **invoice_data},
        ),
        await send_fn(entity_type="Invoice", entity_id="2001", email="client@example.com"),
    ]

    receipts_with_url = [r for r in results if r.receipt is not None and r.receipt.url]
    assert receipts_with_url, "expected QB tools to populate URL receipts"
    for result in results:
        if result.receipt is not None and result.receipt.url:
            assert result.receipt.url not in result.content, (
                "tool result inlined receipt URL into content "
                f"(content={result.content!r}, url={result.receipt.url!r})"
            )
