import logging

from fastapi import APIRouter
from sqlalchemy import text

from backend.app.database import SessionLocal
from backend.app.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    db_status = "ok"
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception:
        logger.exception("Health check: database unreachable")
        db_status = "error"

    status = "ok" if db_status == "ok" else "degraded"
    return HealthResponse(status=status, database=db_status)
