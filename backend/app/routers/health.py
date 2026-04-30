import logging

from fastapi import APIRouter
from sqlalchemy import text

from backend.app.database import SessionLocal
from backend.app.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Full health check: process is up AND can reach the database.

    Use for ops dashboards and richer monitoring. NOT recommended as the
    deployment platform's healthcheck path: a sync DB call from an async
    handler can block the event loop, and during an incident a healthcheck
    that waits on the same DB can pile up alongside whatever already broke.
    Use ``/health/live`` for that.
    """
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


@router.get("/health/live", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    """Liveness probe: the process is up and the event loop is responsive.

    No DB hit, no external calls. Returns instantly when the worker can
    process requests. Designed for the deployment platform's healthcheck
    so a stuck DB / external dep / slow query does not also block the
    healthcheck and prevent traffic from rolling to a fresh container.
    Use ``/health`` for the deeper "is the system actually working" check.
    """
    return HealthResponse(status="ok", database="not_checked")
