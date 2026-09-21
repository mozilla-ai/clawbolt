"""Admin console: deployment stats and version metadata for the overview card."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.app.auth.admin_dep import get_current_admin
from backend.app.channels import is_bluebubbles_configured
from backend.app.config import settings
from backend.app.models import (
    User,
)
from backend.app.schemas.admin import (
    AdminStatsResponse,
    AdminVersionResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.version import get_version_info

router = APIRouter()


@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_STATS)),
) -> AdminStatsResponse:
    """Return the messaging configuration needed by the admin overview."""
    return AdminStatsResponse(
        telegram_configured=bool(settings.telegram_bot_token),
        bluebubbles_configured=is_bluebubbles_configured(),
        twilio_configured=bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret
        ),
    )


@router.get("/version", response_model=AdminVersionResponse)
async def get_admin_version(
    _admin: User = Depends(get_current_admin),
) -> AdminVersionResponse:
    """Build metadata for the admin overview card and the client's auto-reload poll."""
    return AdminVersionResponse(**get_version_info())
