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
        elif entity_type == "Item":
            result["Name"] = data.get("Name", "")
        else:
            result["DocNumber"] = f"10{self._next_id}"
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        self.created.append((entity_type, data))
        return result

    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {**data}
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        elif entity_type == "Item":
            result["Name"] = data.get("Name", "")
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
    # Content is terse and data-only (no "Customer created in QuickBooks"
    # phrasing that the LLM would bullet-point alongside the receipt).
    assert result.content.startswith("ok")
    assert "Name: New Customer LLC" in result.content
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
    assert result.content.startswith("ok")
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
    assert result.content.startswith("ok")
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
    assert result.content.startswith("ok")
    _, body = svc.created[0]
    assert body["LinkedTxn"][0]["TxnId"] == "42"
    assert body["LinkedTxn"][0]["TxnType"] == "Estimate"


# ---------------------------------------------------------------------------
# qb_create - Item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_create_item() -> None:
    """Create a service Item in QuickBooks."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Item",
        data={
            "Name": "Materials",
            "Type": "Service",
            "IncomeAccountRef": {"value": "1", "name": "Services"},
        },
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "Name: Materials" in result.content
    assert len(svc.created) == 1
    entity_type, body = svc.created[0]
    assert entity_type == "Item"
    assert body["Name"] == "Materials"
    assert body["Type"] == "Service"
    assert body["IncomeAccountRef"]["value"] == "1"


@pytest.mark.asyncio()
async def test_qb_create_item_inventory() -> None:
    """Create an inventory Item with QtyOnHand."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_create")

    result = await fn(
        entity_type="Item",
        data={
            "Name": "Drywall Sheet 4x8",
            "Type": "Inventory",
            "UnitPrice": 12.50,
            "QtyOnHand": 100,
            "IncomeAccountRef": {"value": "1", "name": "Services"},
        },
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "Name: Drywall Sheet 4x8" in result.content
    _, body = svc.created[0]
    assert body["QtyOnHand"] == 100


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
    assert result.content.startswith("ok")
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
    assert result.content.startswith("ok")
    assert "Name: John Smith" in result.content
    _, body = svc.updated[0]
    assert body["PrimaryPhone"]["FreeFormNumber"] == "555-9999"


# ---------------------------------------------------------------------------
# qb_update - Item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_qb_update_item() -> None:
    """Update an Item's name and price."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_update")

    result = await fn(
        entity_type="Item",
        data={
            "Id": "1",
            "SyncToken": "0",
            "Name": "Materials (updated)",
            "Type": "Service",
            "IncomeAccountRef": {"value": "1", "name": "Services"},
        },
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "Id: 1" in result.content
    assert "Name: Materials (updated)" in result.content
    assert len(svc.updated) == 1
    entity_type, body = svc.updated[0]
    assert entity_type == "Item"
    assert body["Name"] == "Materials (updated)"
    assert body["SyncToken"] == "0"


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
    # Content stays terse and data-only; the recipient and action verb
    # live in the ToolReceipt, not in the LLM-facing content. Pinned by
    # the dedicated anti-mimicry test below.
    assert result.content.startswith("ok")
    assert "Id: 42" in result.content
    assert svc.sent == [("Invoice", "42", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_estimate_success() -> None:
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Estimate", entity_id="2001", email="client@example.com")

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "Id: 2001" in result.content
    assert svc.sent == [("Estimate", "2001", "client@example.com")]


@pytest.mark.asyncio()
async def test_qb_send_content_does_not_invite_receipt_mimicry() -> None:
    """The LLM-facing content for qb_send must not carry the action verb
    or recipient that the auto-receipt already renders.

    Regression for a 2026-05-13 production observation: a contractor's
    invoice email turned into a double-bullet in the iMessage reply.

    Final body shipped:
        Sent.

        - Sent QuickBooks invoice 573 to <email>

        - Emailed QuickBooks invoice to <email>
          app.qbo.intuit.com/app/invoice?txnId=573

    The first bullet was LLM-fabricated prose mimicking the tool result
    text (``"Invoice 573 sent to <email> via QuickBooks."``); the second
    was the auto-appended ToolReceipt. Mirrors the CompanyCam
    anti-mimicry guard from #1069 but expands it to the recipient and
    action verb, since those (not the URL) were the mimic trigger here.
    """
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    fn = _get_tool(tools, "qb_send")

    result = await fn(entity_type="Invoice", entity_id="573", email="paula@example.com")
    assert result.is_error is False
    assert result.receipt is not None

    # The receipt's URL must not appear in content (the existing #1069
    # guard, now a per-tool invariant). FakeQBService omits the URL, so
    # only assert when a URL is actually rendered.
    if result.receipt.url is not None:
        assert result.receipt.url not in result.content, (
            f"content leaked receipt URL: {result.content!r}"
        )
    # The recipient (target) must not appear in content. Inlining the
    # email teaches the model to bullet-point it in prose.
    assert "paula@example.com" not in result.content, (
        f"content leaked recipient email: {result.content!r}"
    )
    # The action verb ("Emailed" / "sent") must not appear in content.
    lowered = result.content.lower()
    for verb in ("sent", "emailed"):
        assert verb not in lowered, f"content uses receipt-shaped verb {verb!r}: {result.content!r}"


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
        elif entity_type == "Item":
            result["Name"] = data.get("Name", "")
        else:
            result["DocNumber"] = f"10{self._next_id}"
            result["TotalAmt"] = sum(line.get("Amount", 0) for line in data.get("Line", []))
        return result

    async def update_entity(self, entity_type: str, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {**data}
        if entity_type == "Customer":
            result["DisplayName"] = data.get("DisplayName", "")
        elif entity_type == "Item":
            result["Name"] = data.get("Name", "")
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


# ---------------------------------------------------------------------------
# Approval prompt: line-item breakdown so users catch billing mistakes
# ---------------------------------------------------------------------------


def _get_description_builder(tools: list, name: str) -> Any:
    for t in tools:
        if t.name == name:
            assert t.approval_policy is not None
            assert t.approval_policy.description_builder is not None
            return t.approval_policy.description_builder
    raise KeyError(f"Tool {name} not found")


def test_qb_create_approval_description_renders_invoice_line_items() -> None:
    """The qb_create approval prompt for an Invoice must show qty x unit
    = line total per item plus a grand total, mirroring the AppFolio fix
    in #1292.
    """
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Invoice",
            "data": {
                "CustomerRef": {"value": "16", "name": "Acme Plumbing"},
                "Line": [
                    {
                        "Amount": 39.07,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Materials reimbursement",
                        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 39.07},
                    },
                    {
                        "Amount": 275.00,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Labor",
                        "SalesItemLineDetail": {"Qty": 5, "UnitPrice": 55.00},
                    },
                ],
            },
        }
    )
    assert "Create Invoice in QuickBooks" in description
    assert "$314.07" in description
    assert "Materials reimbursement" in description
    assert "qty 1 x $39.07 = $39.07" in description
    assert "Labor" in description
    assert "qty 5 x $55.00 = $275.00" in description


def test_qb_create_approval_description_estimate_uses_same_breakdown() -> None:
    """Estimate payloads share Invoice's line shape and must render
    the same breakdown."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Estimate",
            "data": {
                "Line": [
                    {
                        "Amount": 600.00,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Labor",
                        "SalesItemLineDetail": {"Qty": 12, "UnitPrice": 50.00},
                    },
                ],
            },
        }
    )
    assert "Create Estimate in QuickBooks for $600.00" in description
    assert "qty 12 x $50.00 = $600.00" in description


def test_qb_create_approval_description_customer_keeps_short_form() -> None:
    """Customer payloads have no Line array; the prompt must stay the
    legacy short form rather than render an empty breakdown."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Customer",
            "data": {"DisplayName": "Acme Plumbing", "PrimaryEmailAddr": {"Address": "x@y.z"}},
        }
    )
    assert description == "Create Customer in QuickBooks"


def test_qb_create_approval_description_item_keeps_short_form() -> None:
    """Item payloads have no Line array; the prompt must stay the
    legacy short form."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Item",
            "data": {
                "Name": "Materials",
                "Type": "Service",
                "IncomeAccountRef": {"value": "1", "name": "Services"},
            },
        }
    )
    assert description == "Create Item in QuickBooks"


def test_qb_create_approval_description_falls_back_without_sales_item_detail() -> None:
    """A line missing SalesItemLineDetail still renders, using Amount alone."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Invoice",
            "data": {
                "Line": [
                    {
                        "Amount": 100.00,
                        "DetailType": "DescriptionOnly",
                        "Description": "Free-form note",
                    },
                ],
            },
        }
    )
    assert "Create Invoice in QuickBooks for $100.00" in description
    assert "$100.00" in description
    # No qty/unit breakdown when the detail block is absent.
    assert "qty" not in description.lower()


def test_qb_create_approval_description_truncates_long_descriptions() -> None:
    """Very long line descriptions are truncated so the prompt stays
    scannable in a chat channel."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    long = "Lorem ipsum dolor sit amet consectetur adipiscing elit, " * 5
    description = builder(
        {
            "entity_type": "Invoice",
            "data": {
                "Line": [
                    {
                        "Amount": 100.0,
                        "DetailType": "SalesItemLineDetail",
                        "Description": long,
                        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 100.0},
                    },
                ],
            },
        }
    )
    assert "..." in description
    assert long not in description


def test_qb_create_approval_description_falls_back_on_malformed_payload() -> None:
    """A malformed payload must not raise from the description builder.

    QBCreateParams validation runs after the approval prompt is rendered,
    so the prompt has to tolerate bad input rather than crash the
    approval flow.
    """
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    # Non-dict line entry should fall back to the short form rather than
    # raise an AttributeError.
    description = builder(
        {
            "entity_type": "Invoice",
            "data": {"Line": ["not-a-dict"]},
        }
    )
    assert description == "Create Invoice in QuickBooks"

    # Non-numeric Amount should also fall back rather than blow up
    # ``float()`` and crash the approval prompt.
    description = builder(
        {
            "entity_type": "Invoice",
            "data": {"Line": [{"Amount": "not-a-number", "Description": "ok"}]},
        }
    )
    assert description == "Create Invoice in QuickBooks"


def test_qb_update_approval_description_includes_entity_id() -> None:
    """The qb_update prompt must call out which Id is being changed so
    an admin reviewing audit logs can trace it. Line item breakdown
    follows the same rules as qb_create."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_update")

    description = builder(
        {
            "entity_type": "Estimate",
            "data": {
                "Id": "2001",
                "SyncToken": "0",
                "Line": [
                    {
                        "Amount": 600.00,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Labor (revised)",
                        "SalesItemLineDetail": {"Qty": 12, "UnitPrice": 50.00},
                    },
                ],
            },
        }
    )
    assert "Update Estimate #2001 in QuickBooks for $600.00" in description
    assert "qty 12 x $50.00 = $600.00" in description


def test_qb_update_approval_description_customer_short_form_includes_id() -> None:
    """For non-itemized entity types (Customer), the qb_update short
    form should still reference the Id being updated."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_update")

    description = builder(
        {
            "entity_type": "Customer",
            "data": {"Id": "100", "SyncToken": "1", "DisplayName": "Acme"},
        }
    )
    assert description == "Update Customer #100 in QuickBooks"


def test_qb_update_approval_description_item_short_form_includes_id() -> None:
    """For non-itemized entity types (Item), the qb_update short
    form should still reference the Id being updated."""
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_update")

    description = builder(
        {
            "entity_type": "Item",
            "data": {
                "Id": "1",
                "SyncToken": "0",
                "Name": "Materials",
                "Type": "Service",
            },
        }
    )
    assert description == "Update Item #1 in QuickBooks"


def test_qb_create_approval_description_uses_thousands_separator() -> None:
    """Currency formatting matches ``_receipt_target``: thousands
    separator on every dollar amount so the same invoice reads the
    same in the approval prompt and the post-write ToolReceipt.
    """
    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Invoice",
            "data": {
                "Line": [
                    {
                        "Amount": 12345.67,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Kitchen remodel",
                        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 12345.67},
                    },
                    {
                        "Amount": 2500.00,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Materials",
                        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 2500.00},
                    },
                ],
            },
        }
    )
    assert "Create Invoice in QuickBooks for $14,845.67" in description
    assert "qty 1 x $12,345.67 = $12,345.67" in description
    assert "qty 1 x $2,500.00 = $2,500.00" in description


def test_qb_create_approval_description_survives_format_approval_message() -> None:
    """The multi-line breakdown must pass through ``format_approval_message``
    intact (line items still visible, header still in place) so the
    user sees the breakdown in their actual channel, not just in a
    unit-test assertion. Belt-and-suspenders for the approval pipeline.
    """
    from backend.app.agent.approval import format_approval_message

    svc = FakeQBService()
    tools = create_quickbooks_tools(svc)
    builder = _get_description_builder(tools, "qb_create")

    description = builder(
        {
            "entity_type": "Invoice",
            "data": {
                "CustomerRef": {"value": "16", "name": "Acme Plumbing"},
                "Line": [
                    {
                        "Amount": 39.07,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Materials reimbursement",
                        "SalesItemLineDetail": {"Qty": 1, "UnitPrice": 39.07},
                    },
                    {
                        "Amount": 275.00,
                        "DetailType": "SalesItemLineDetail",
                        "Description": "Labor",
                        "SalesItemLineDetail": {"Qty": 5, "UnitPrice": 55.00},
                    },
                ],
            },
        }
    )
    prompt = format_approval_message("qb_create", description)

    # The header and every line item must survive the wrapping.
    assert "Create Invoice in QuickBooks for $314.07" in prompt
    assert "qty 1 x $39.07 = $39.07" in prompt
    assert "qty 5 x $55.00 = $275.00" in prompt
    # And the prompt still ends with the four-option menu so the
    # multi-line description has not stomped the reply instructions.
    assert "yes: allow this once" in prompt
    assert "never: deny and remember" in prompt
