"""Database-backed client and estimate stores.

Replaces ClientStore and EstimateStore from file_store.py. Uses Client,
Estimate, and EstimateLineItem ORM models for persistence, while keeping
ClientData, EstimateData, and EstimateLineItemData Pydantic models as
in-memory DTOs.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from backend.app.agent.dto import (
    ClientData,
    EstimateData,
    EstimateLineItemData,
    InvoiceData,
    InvoiceLineItemData,
    _unique_slug,
    make_client_slug,
)
from backend.app.database import SessionLocal, db_session
from backend.app.enums import EstimateStatus, InvoiceStatus
from backend.app.models import Client, Estimate, EstimateLineItem, Invoice, InvoiceLineItem

logger = logging.getLogger(__name__)

# Re-export so callers can import from here
__all__ = ["ClientStore", "EstimateStore", "InvoiceStore", "make_client_slug"]


# ---------------------------------------------------------------------------
# ORM -> DTO converters
# ---------------------------------------------------------------------------


def _client_to_data(c: Client) -> ClientData:
    return ClientData(
        id=c.id,
        name=c.name,
        phone=c.phone,
        email=c.email,
        address=c.address,
        notes=c.notes,
        created_at=c.created_at.isoformat() if c.created_at else "",
    )


def _invoice_to_data(inv: Invoice, items: list[InvoiceLineItem] | None = None) -> InvoiceData:
    line_items = items if items is not None else []
    return InvoiceData(
        id=inv.id,
        user_id=inv.user_id,
        client_id=inv.client_id or "",
        description=inv.description,
        total_amount=inv.total_amount,
        status=inv.status,
        pdf_url=inv.pdf_url,
        storage_path=inv.storage_path,
        due_date=inv.due_date,
        estimate_id=inv.estimate_id,
        notes=inv.notes,
        line_items=[
            InvoiceLineItemData(
                id=li.id,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
            for li in line_items
        ],
        created_at=inv.created_at.isoformat() if inv.created_at else "",
    )


def _estimate_to_data(e: Estimate, items: list[EstimateLineItem] | None = None) -> EstimateData:
    line_items = items if items is not None else []
    return EstimateData(
        id=e.id,
        user_id=e.user_id,
        client_id=e.client_id or "",
        description=e.description,
        total_amount=e.total_amount,
        status=e.status,
        pdf_url=e.pdf_url,
        storage_path=e.storage_path,
        line_items=[
            EstimateLineItemData(
                id=li.id,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
            for li in line_items
        ],
        created_at=e.created_at.isoformat() if e.created_at else "",
    )


# ---------------------------------------------------------------------------
# ClientStore
# ---------------------------------------------------------------------------


class ClientStore:
    """Database-backed client storage using Client ORM model."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def list_all(self) -> list[ClientData]:
        """List all clients for this user."""
        db = SessionLocal()
        try:
            clients = (
                db.query(Client).filter_by(user_id=self.user_id).order_by(Client.created_at).all()
            )
            return [_client_to_data(c) for c in clients]
        finally:
            db.close()

    async def get(self, client_id: str) -> ClientData | None:
        """Get a client by ID."""
        db = SessionLocal()
        try:
            c = db.query(Client).filter_by(id=client_id, user_id=self.user_id).first()
            return _client_to_data(c) if c else None
        finally:
            db.close()

    async def create(
        self,
        name: str = "",
        phone: str = "",
        email: str = "",
        address: str = "",
        notes: str = "",
        folder_scheme: str = "",
    ) -> ClientData:
        """Create a new client with a slug-based ID."""
        with db_session() as db:
            # Build slug
            base_slug = make_client_slug(name, address, folder_scheme)
            if not base_slug:
                base_slug = "client"

            # Get existing IDs for uniqueness (lock rows to prevent races)
            existing_ids = {
                row[0]
                for row in db.query(Client.id)
                .filter_by(user_id=self.user_id)
                .with_for_update()
                .all()
            }
            cid = _unique_slug(base_slug, existing_ids)

            client = Client(
                id=cid,
                user_id=self.user_id,
                name=name,
                phone=phone,
                email=email,
                address=address,
                notes=notes,
            )
            db.add(client)
            db.commit()
            db.refresh(client)
            return _client_to_data(client)


# ---------------------------------------------------------------------------
# EstimateStore
# ---------------------------------------------------------------------------


class EstimateStore:
    """Database-backed estimate storage using Estimate and EstimateLineItem ORM models."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def list_all(self) -> list[EstimateData]:
        """List all estimates for this user."""
        db = SessionLocal()
        try:
            estimates = (
                db.query(Estimate)
                .filter_by(user_id=self.user_id)
                .order_by(Estimate.created_at)
                .all()
            )
            result = []
            for e in estimates:
                items = db.query(EstimateLineItem).filter_by(estimate_id=e.id).all()
                result.append(_estimate_to_data(e, items))
            return result
        finally:
            db.close()

    async def get(self, estimate_id: str) -> EstimateData | None:
        """Get an estimate by ID."""
        db = SessionLocal()
        try:
            e = db.query(Estimate).filter_by(id=estimate_id, user_id=self.user_id).first()
            if e is None:
                return None
            items = db.query(EstimateLineItem).filter_by(estimate_id=e.id).all()
            return _estimate_to_data(e, items)
        finally:
            db.close()

    def _next_estimate_number(self, db: object) -> int:
        """Get the next sequential estimate number (globally unique across all users)."""
        from sqlalchemy.orm import Session as SASession

        assert isinstance(db, SASession)
        # Filter to only EST-* rows to reduce lock scope
        rows = db.query(Estimate.id).filter(Estimate.id.like("EST-%")).with_for_update().all()
        max_num = 0
        for (eid,) in rows:
            with contextlib.suppress(ValueError):
                max_num = max(max_num, int(eid[4:]))
        return max_num + 1

    async def create(
        self,
        description: str = "",
        total_amount: float = 0.0,
        status: str = EstimateStatus.DRAFT,
        client_id: str | None = None,
        line_items: list[dict[str, Any]] | None = None,
    ) -> EstimateData:
        """Create a new estimate with line items."""
        with db_session() as db:
            num = self._next_estimate_number(db)
            eid = f"EST-{num:04d}"

            estimate = Estimate(
                id=eid,
                user_id=self.user_id,
                client_id=client_id or None,
                description=description,
                total_amount=total_amount,
                status=status,
            )
            db.add(estimate)
            db.flush()

            orm_items: list[EstimateLineItem] = []
            for i, li in enumerate(line_items or [], 1):
                item = EstimateLineItem(
                    id=f"{eid}-{i}",
                    estimate_id=eid,
                    description=str(li.get("description", "")),
                    quantity=float(li.get("quantity", 1)),
                    unit_price=float(li.get("unit_price", 0)),
                    total=float(li.get("total", 0)),
                )
                db.add(item)
                orm_items.append(item)

            db.commit()
            db.refresh(estimate)
            return _estimate_to_data(estimate, orm_items)

    async def update(self, estimate_id: str, **fields: Any) -> EstimateData | None:
        """Update an estimate's fields."""
        with db_session() as db:
            e = db.query(Estimate).filter_by(id=estimate_id, user_id=self.user_id).first()
            if e is None:
                return None

            for key, value in fields.items():
                if value is not None and hasattr(e, key):
                    setattr(e, key, value)
            db.commit()
            db.refresh(e)

            items = db.query(EstimateLineItem).filter_by(estimate_id=e.id).all()
            return _estimate_to_data(e, items)


# ---------------------------------------------------------------------------
# InvoiceStore
# ---------------------------------------------------------------------------


class InvoiceStore:
    """Database-backed invoice storage using Invoice and InvoiceLineItem ORM models."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def list_all(self) -> list[InvoiceData]:
        """List all invoices for this user."""
        db = SessionLocal()
        try:
            invoices = (
                db.query(Invoice).filter_by(user_id=self.user_id).order_by(Invoice.created_at).all()
            )
            result = []
            for inv in invoices:
                items = db.query(InvoiceLineItem).filter_by(invoice_id=inv.id).all()
                result.append(_invoice_to_data(inv, items))
            return result
        finally:
            db.close()

    async def get(self, invoice_id: str) -> InvoiceData | None:
        """Get an invoice by ID."""
        db = SessionLocal()
        try:
            inv = db.query(Invoice).filter_by(id=invoice_id, user_id=self.user_id).first()
            if inv is None:
                return None
            items = db.query(InvoiceLineItem).filter_by(invoice_id=inv.id).all()
            return _invoice_to_data(inv, items)
        finally:
            db.close()

    def _next_invoice_number(self, db: object) -> int:
        """Get the next sequential invoice number (globally unique across all users)."""
        from sqlalchemy.orm import Session as SASession

        assert isinstance(db, SASession)
        # Filter to only INV-* rows to reduce lock scope
        rows = db.query(Invoice.id).filter(Invoice.id.like("INV-%")).with_for_update().all()
        max_num = 0
        for (iid,) in rows:
            with contextlib.suppress(ValueError):
                max_num = max(max_num, int(iid[4:]))
        return max_num + 1

    async def create(
        self,
        description: str = "",
        total_amount: float = 0.0,
        status: str = InvoiceStatus.DRAFT,
        client_id: str | None = None,
        line_items: list[dict[str, Any]] | None = None,
        due_date: str | None = None,
        estimate_id: str | None = None,
        notes: str = "",
    ) -> InvoiceData:
        """Create a new invoice with line items."""
        with db_session() as db:
            num = self._next_invoice_number(db)
            iid = f"INV-{num:04d}"

            invoice = Invoice(
                id=iid,
                user_id=self.user_id,
                client_id=client_id or None,
                description=description,
                total_amount=total_amount,
                status=status,
                due_date=due_date,
                estimate_id=estimate_id,
                notes=notes,
            )
            db.add(invoice)
            db.flush()

            orm_items: list[InvoiceLineItem] = []
            for i, li in enumerate(line_items or [], 1):
                item = InvoiceLineItem(
                    id=f"{iid}-{i}",
                    invoice_id=iid,
                    description=str(li.get("description", "")),
                    quantity=float(li.get("quantity", 1)),
                    unit_price=float(li.get("unit_price", 0)),
                    total=float(li.get("total", 0)),
                )
                db.add(item)
                orm_items.append(item)

            db.commit()
            db.refresh(invoice)
            return _invoice_to_data(invoice, orm_items)

    async def update(self, invoice_id: str, **fields: Any) -> InvoiceData | None:
        """Update an invoice's fields."""
        with db_session() as db:
            inv = db.query(Invoice).filter_by(id=invoice_id, user_id=self.user_id).first()
            if inv is None:
                return None

            for key, value in fields.items():
                if value is not None and hasattr(inv, key):
                    setattr(inv, key, value)
            db.commit()
            db.refresh(inv)

            items = db.query(InvoiceLineItem).filter_by(invoice_id=inv.id).all()
            return _invoice_to_data(inv, items)
