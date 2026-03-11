import re
from typing import Any

from backend.app.services.quickbooks_service import QuickBooksService

# Sample data for the mock QBO sandbox.
_CUSTOMERS: list[dict[str, Any]] = [
    {
        "Id": "100",
        "DisplayName": "John Smith",
        "PrimaryEmailAddr": {"Address": "john@example.com"},
        "PrimaryPhone": {"FreeFormNumber": "555-0100"},
        "Balance": 0,
    },
    {
        "Id": "101",
        "DisplayName": "Jane Doe",
        "PrimaryEmailAddr": {"Address": "jane@example.com"},
        "PrimaryPhone": {"FreeFormNumber": "555-0101"},
        "Balance": 250.00,
    },
]

_INVOICES: list[dict[str, Any]] = [
    {
        "Id": "1001",
        "DocNumber": "INV-1001",
        "CustomerRef": {"value": "100", "name": "John Smith"},
        "TotalAmt": 500.00,
        "Balance": 0,
        "DueDate": "2026-02-15",
        "TxnDate": "2026-01-15",
        "EmailStatus": "EmailSent",
    },
    {
        "Id": "1002",
        "DocNumber": "INV-1002",
        "CustomerRef": {"value": "101", "name": "Jane Doe"},
        "TotalAmt": 1250.00,
        "Balance": 250.00,
        "DueDate": "2026-03-01",
        "TxnDate": "2026-02-01",
        "EmailStatus": "NotSet",
    },
]

_ESTIMATES: list[dict[str, Any]] = [
    {
        "Id": "2001",
        "DocNumber": "EST-2001",
        "CustomerRef": {"value": "100", "name": "John Smith"},
        "TotalAmt": 3200.00,
        "TxnDate": "2026-01-10",
        "ExpirationDate": "2026-02-10",
        "TxnStatus": "Accepted",
    },
]

_ITEMS: list[dict[str, Any]] = [
    {
        "Id": "1",
        "Name": "Drywall Sheet 4x8",
        "Description": "Standard 1/2 inch drywall sheet",
        "UnitPrice": 12.50,
        "Type": "Inventory",
    },
]

_ENTITY_DATA: dict[str, list[dict[str, Any]]] = {
    "Customer": _CUSTOMERS,
    "Invoice": _INVOICES,
    "Estimate": _ESTIMATES,
    "Item": _ITEMS,
}


class MockQuickBooksService(QuickBooksService):
    """In-memory mock QuickBooks service for testing.

    Supports a subset of QBO query syntax: entity routing, WHERE with simple
    conditions (=, LIKE), and MAXRESULTS.
    """

    async def query(self, query_str: str) -> list[dict[str, Any]]:
        # Parse entity name from "SELECT ... FROM <Entity> ..."
        match = re.search(r"FROM\s+(\w+)", query_str, re.IGNORECASE)
        if not match:
            return []
        entity = match.group(1)
        rows = list(_ENTITY_DATA.get(entity, []))

        # Simple WHERE field = 'value' filter
        eq_match = re.search(r"WHERE\s+(\w+)\s*=\s*'([^']*)'", query_str, re.IGNORECASE)
        if eq_match:
            field, value = eq_match.group(1), eq_match.group(2)
            rows = [r for r in rows if str(r.get(field, "")) == value]

        # Simple WHERE field LIKE '%value%' filter
        like_match = re.search(r"WHERE\s+(\w+)\s+LIKE\s+'%([^%]*)%'", query_str, re.IGNORECASE)
        if like_match:
            field, value = like_match.group(1), like_match.group(2)
            rows = [r for r in rows if value.lower() in str(r.get(field, "")).lower()]

        # MAXRESULTS
        max_match = re.search(r"MAXRESULTS\s+(\d+)", query_str, re.IGNORECASE)
        if max_match:
            rows = rows[: int(max_match.group(1))]

        return rows
