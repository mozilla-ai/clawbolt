"""Connect endpoints for web-form integrations (ServiceTitan, AppFolio).

OAuth integrations authorize through ``/oauth/...``. ServiceTitan and
AppFolio Vendor Portal authenticate with pasted secrets instead: a tenant's
ServiceTitan client credentials, or AppFolio's single-use magic link. Those
secrets must never travel through a chat thread, where they would persist in
the user's message history (issue #1337). These endpoints let the web app
collect them over an authenticated HTTPS session and persist them
server-side.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.app.auth.dependencies import get_current_user
from backend.app.integrations.appfolio_vendor import auth as appfolio_auth
from backend.app.integrations.appfolio_vendor.auth import MagicLinkError
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    connect_via_magic_link,
)
from backend.app.integrations.servicetitan import auth as servicetitan_auth
from backend.app.integrations.servicetitan.auth import ServiceTitanAuthError
from backend.app.models import User
from backend.app.schemas import (
    AppFolioConnectRequest,
    IntegrationConnectionResponse,
    ServiceTitanConnectRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/integrations/servicetitan/connect",
    response_model=IntegrationConnectionResponse,
)
async def connect_servicetitan(
    body: ServiceTitanConnectRequest,
    current_user: User = Depends(get_current_user),
) -> IntegrationConnectionResponse:
    """Validate ServiceTitan client credentials and persist them."""
    try:
        await servicetitan_auth.connect_credentials(
            current_user.id,
            tenant_id=body.tenant_id,
            client_id=body.client_id,
            client_secret=body.client_secret,
        )
    except ServiceTitanAuthError as exc:
        logger.warning("ServiceTitan connect failed for user=%s: %s", current_user.id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IntegrationConnectionResponse(integration="servicetitan", connected=True)


@router.delete(
    "/integrations/servicetitan",
    response_model=IntegrationConnectionResponse,
)
async def disconnect_servicetitan(
    current_user: User = Depends(get_current_user),
) -> IntegrationConnectionResponse:
    """Remove the user's stored ServiceTitan credential."""
    if not await servicetitan_auth.is_connected(current_user.id):
        raise HTTPException(status_code=404, detail="ServiceTitan is not connected")
    await servicetitan_auth.clear_credentials(current_user.id)
    return IntegrationConnectionResponse(integration="servicetitan", connected=False)


@router.post(
    "/integrations/appfolio_vendor/connect",
    response_model=IntegrationConnectionResponse,
)
async def connect_appfolio(
    body: AppFolioConnectRequest,
    current_user: User = Depends(get_current_user),
) -> IntegrationConnectionResponse:
    """Exchange a pasted AppFolio magic link for a credential and persist it."""
    try:
        await connect_via_magic_link(current_user.id, body.magic_link)
    except MagicLinkError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not read the magic link: {exc}. Paste the full link from your"
                " AppFolio sign-in email, including the part after 'magic_link_token='."
            ),
        ) from exc
    except AppFolioError as exc:
        logger.warning("AppFolio connect failed for user=%s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=400,
            detail=(
                f"AppFolio rejected the magic link: {exc}. Magic links are single-use and"
                " expire quickly, so request a fresh one from vendor.appfolio.com and try again."
            ),
        ) from exc
    return IntegrationConnectionResponse(integration="appfolio_vendor", connected=True)


@router.delete(
    "/integrations/appfolio_vendor",
    response_model=IntegrationConnectionResponse,
)
async def disconnect_appfolio(
    current_user: User = Depends(get_current_user),
) -> IntegrationConnectionResponse:
    """Remove the user's stored AppFolio credential."""
    if not await appfolio_auth.is_connected(current_user.id):
        raise HTTPException(status_code=404, detail="AppFolio Vendor Portal is not connected")
    await appfolio_auth.clear_credential(current_user.id)
    return IntegrationConnectionResponse(integration="appfolio_vendor", connected=False)
