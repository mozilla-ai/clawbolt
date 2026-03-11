from typing import Any

from backend.app.services.quickbooks_service import QuickBooksService


class MockQuickBooksService(QuickBooksService):
    """In-memory mock QuickBooks service for testing."""

    def __init__(self) -> None:
        self.invoices: list[dict[str, Any]] = [
            {
                "id": "1001",
                "doc_number": "INV-1001",
                "customer_name": "John Smith",
                "total": 500.00,
                "balance": 0,
                "due_date": "2026-02-15",
                "date": "2026-01-15",
                "email_status": "EmailSent",
            },
            {
                "id": "1002",
                "doc_number": "INV-1002",
                "customer_name": "Jane Doe",
                "total": 1250.00,
                "balance": 250.00,
                "due_date": "2026-03-01",
                "date": "2026-02-01",
                "email_status": "NotSet",
            },
        ]
        self.estimates: list[dict[str, Any]] = [
            {
                "id": "2001",
                "doc_number": "EST-2001",
                "customer_name": "John Smith",
                "total": 3200.00,
                "date": "2026-01-10",
                "expiry_date": "2026-02-10",
                "status": "Accepted",
            },
            {
                "id": "2002",
                "doc_number": "EST-2002",
                "customer_name": "Jane Doe",
                "total": 750.00,
                "date": "2026-02-20",
                "expiry_date": "2026-03-20",
                "status": "Pending",
            },
        ]

    async def list_invoices(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        if customer_name:
            return [
                inv
                for inv in self.invoices
                if customer_name.lower() in inv["customer_name"].lower()
            ]
        return list(self.invoices)

    async def list_estimates(self, customer_name: str | None = None) -> list[dict[str, Any]]:
        if customer_name:
            return [
                est
                for est in self.estimates
                if customer_name.lower() in est["customer_name"].lower()
            ]
        return list(self.estimates)
