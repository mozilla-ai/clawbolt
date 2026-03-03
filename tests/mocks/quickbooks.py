from typing import Any

from backend.app.services.quickbooks_service import QuickBooksService


class MockQuickBooksService(QuickBooksService):
    """In-memory mock QuickBooks service for testing."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = [
            {
                "id": "1",
                "name": "Drywall Sheet 4x8",
                "description": "Standard 1/2 inch drywall sheet",
                "unit_price": 12.50,
                "type": "Inventory",
            },
            {
                "id": "2",
                "name": "Labor - General",
                "description": "General labor per hour",
                "unit_price": 75.00,
                "type": "Service",
            },
        ]
        self.customers: list[dict[str, Any]] = [
            {
                "id": "100",
                "display_name": "John Smith",
                "primary_email": "john@example.com",
                "primary_phone": "555-0100",
                "balance": 0,
            },
            {
                "id": "101",
                "display_name": "Jane Doe",
                "primary_email": "jane@example.com",
                "primary_phone": "555-0101",
                "balance": 250.00,
            },
        ]
        self.invoices: dict[str, dict[str, Any]] = {}
        self.estimates: dict[str, dict[str, Any]] = {}
        self.sent_invoices: list[str] = []
        self._next_invoice_id = 1000
        self._next_estimate_id = 2000

    async def list_items(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            return [item for item in self.items if query.lower() in item["name"].lower()]
        return list(self.items)

    async def list_customers(self, query: str | None = None) -> list[dict[str, Any]]:
        if query:
            return [c for c in self.customers if query.lower() in c["display_name"].lower()]
        return list(self.customers)

    async def create_invoice(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        invoice_id = str(self._next_invoice_id)
        self._next_invoice_id += 1
        total = sum(float(li.get("amount", 0)) for li in line_items)
        invoice = {
            "id": invoice_id,
            "doc_number": f"INV-{invoice_id}",
            "customer_id": customer_id,
            "line_items": line_items,
            "total": total,
            "balance": total,
            "status": "created",
        }
        self.invoices[invoice_id] = invoice
        return invoice

    async def send_invoice(self, invoice_id: str) -> dict[str, Any]:
        if invoice_id not in self.invoices:
            msg = f"Invoice {invoice_id} not found"
            raise ValueError(msg)
        self.sent_invoices.append(invoice_id)
        self.invoices[invoice_id]["status"] = "sent"
        return {
            "id": invoice_id,
            "email_status": "EmailSent",
            "status": "sent",
        }

    async def create_estimate(
        self,
        customer_id: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        estimate_id = str(self._next_estimate_id)
        self._next_estimate_id += 1
        total = sum(float(li.get("amount", 0)) for li in line_items)
        estimate = {
            "id": estimate_id,
            "doc_number": f"EST-{estimate_id}",
            "customer_id": customer_id,
            "line_items": line_items,
            "total": total,
            "status": "created",
        }
        self.estimates[estimate_id] = estimate
        return estimate
