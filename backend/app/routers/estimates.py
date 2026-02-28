"""Endpoints for estimate PDF serving."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()

PDF_DIR = Path("data/estimates")


@router.get("/estimates/{estimate_id}/pdf")
async def serve_estimate_pdf(estimate_id: int) -> Response:
    """Serve a generated estimate PDF by estimate ID."""
    pdf_path = PDF_DIR / f"{estimate_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Estimate PDF not found")

    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=estimate-{estimate_id}.pdf"},
    )
