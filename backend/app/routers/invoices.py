"""Endpoints for invoice PDF serving."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.app.agent.client_db import InvoiceStore
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

PDF_BASE_DIR = Path(settings.pdf_storage_dir)


@router.get("/invoices/{invoice_id}/pdf")
async def serve_invoice_pdf(
    invoice_id: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Serve a generated invoice PDF by invoice ID."""
    # Verify the invoice exists and belongs to the current user
    invoice_store = InvoiceStore(current_user.id)
    invoice = await invoice_store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Look for PDF under client subfolder, then fallback to flat path
    client_folder = invoice.client_id or "unsorted"
    pdf_path = PDF_BASE_DIR / str(current_user.id) / client_folder / f"{invoice_id}.pdf"
    if not pdf_path.exists():
        pdf_path = PDF_BASE_DIR / str(current_user.id) / f"{invoice_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Invoice PDF not found")

    content = await asyncio.to_thread(pdf_path.read_bytes)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=invoice-{invoice_id}.pdf"},
    )
