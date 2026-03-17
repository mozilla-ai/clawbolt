"""Endpoints for estimate PDF serving."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.app.agent.client_db import EstimateStore
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

PDF_BASE_DIR = Path(settings.pdf_storage_dir)


@router.get("/estimates/{estimate_id}/pdf")
async def serve_estimate_pdf(
    estimate_id: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Serve a generated estimate PDF by estimate ID."""
    # Verify the estimate exists and belongs to the current user
    estimate_store = EstimateStore(current_user.id)
    estimate = await estimate_store.get(estimate_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")

    # Look for PDF under client subfolder, then fallback to flat path
    client_folder = estimate.client_id or "unsorted"
    pdf_path = PDF_BASE_DIR / str(current_user.id) / client_folder / f"{estimate_id}.pdf"
    if not pdf_path.exists():
        # Fallback: old-style flat path for pre-existing PDFs
        pdf_path = PDF_BASE_DIR / str(current_user.id) / f"{estimate_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Estimate PDF not found")

    content = await asyncio.to_thread(pdf_path.read_bytes)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=estimate-{estimate_id}.pdf"},
    )
