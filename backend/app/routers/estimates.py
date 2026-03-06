"""Endpoints for estimate PDF serving."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.app.agent.file_store import ContractorData, EstimateStore
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

PDF_BASE_DIR = Path(settings.pdf_storage_dir)


@router.get("/estimates/{estimate_id}/pdf")
async def serve_estimate_pdf(
    estimate_id: int,
    current_user: ContractorData = Depends(get_current_user),
) -> Response:
    """Serve a generated estimate PDF by estimate ID."""
    # Verify the estimate exists and belongs to the current user
    estimate_store = EstimateStore(current_user.id)
    estimate = await estimate_store.get(estimate_id)
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")

    pdf_path = PDF_BASE_DIR / str(current_user.id) / f"{estimate_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Estimate PDF not found")

    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=estimate-{estimate_id}.pdf"},
    )
