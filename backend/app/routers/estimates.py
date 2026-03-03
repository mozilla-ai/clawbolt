"""Endpoints for estimate PDF serving."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.auth.scoping import get_user_estimate
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import Contractor

logger = logging.getLogger(__name__)

router = APIRouter()

PDF_BASE_DIR = Path(settings.pdf_storage_dir)


@router.get("/estimates/{estimate_id}/pdf")
async def serve_estimate_pdf(
    estimate_id: int,
    db: Session = Depends(get_db),
    current_user: Contractor = Depends(get_current_user),
) -> Response:
    """Serve a generated estimate PDF by estimate ID."""
    # Verify the estimate exists and belongs to the current user
    get_user_estimate(db, current_user, estimate_id)

    # NOTE: This path includes {contractor_id}/ which is a breaking change for
    # any pre-existing estimate PDFs stored at data/estimates/{id}.pdf. Since the
    # project is pre-production, old PDFs must be migrated manually if needed.
    pdf_path = PDF_BASE_DIR / str(current_user.id) / f"{estimate_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Estimate PDF not found")

    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=estimate-{estimate_id}.pdf"},
    )
