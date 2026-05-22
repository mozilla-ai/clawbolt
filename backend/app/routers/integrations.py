"""Web-based credential input endpoints for non-OAuth integrations.

AppFolio Vendor Portal and ServiceTitan use credential-paste flows that
should happen in the Clawbolt web app rather than in chat, so users
never paste secure secrets (magic-link tokens, Client IDs, Client
Secrets) into an iMessage thread where they would persist in chat
history.

These endpoints accept the credential values from a web form, process
them identically to the (now deprecated) chat-based auth tools, and
persist the resulting tokens.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.integrations.appfolio_vendor.auth import (
    MagicLinkError,
    extract_magic_link_token,
    save_credential,
    upsert_fingerprint,
)
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    exchange_magic_link,
)
from backend.app.integrations.servicetitan.auth import (
    ServiceTitanAuthError,
    mint_access_token,
    save_credentials,
)
from backend.app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# AppFolio Vendor Portal - web-based magic-link connect
# ---------------------------------------------------------------------------


class AppFolioConnectRequest(BaseModel):
    """Request body for connecting AppFolio via the web UI.

    The user pastes the magic-link URL they received from AppFolio email;
    the backend extracts the token, exchanges it for a Bearer JWT, and
    persists the credential.
    """

    magic_link: str = Field(
        description="The full magic-link URL from AppFolio email, or just the token"
    )


class ConnectResponse(BaseModel):
    """Response for a successful web-based connect."""

    status: str = "connected"
    message: str = ""


@router.post("/integrations/appfolio_vendor/connect")
async def appfolio_web_connect(
    body: AppFolioConnectRequest,
    current_user: User = Depends(get_current_user),
) -> ConnectResponse:
    """Connect AppFolio Vendor Portal via a magic link pasted in the web UI.

    Extracts the magic-link token from the user's input, exchanges it
    for a Bearer JWT via the AppFolio OAuth2 token endpoint, and
    persists the credential so the agent's AppFolio tools become
    available.
    """
    try:
        token = extract_magic_link_token(body.magic_link)
    except MagicLinkError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read magic link: {exc}",
        ) from exc

    fingerprint = await upsert_fingerprint(current_user.id)
    try:
        result = await exchange_magic_link(magic_link_token=token)
    except AppFolioError as exc:
        logger.warning("AppFolio OAuth exchange failed for user %s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=400,
            detail=f"AppFolio rejected the magic link: {exc}",
        ) from exc

    await save_credential(
        user_id=current_user.id,
        jwt=result.jwt,
        fingerprint=fingerprint,
        customer_ids=result.customer_ids,
        refresh_token=result.refresh_token,
    )

    logger.info("User %s connected AppFolio Vendor Portal via web UI", current_user.id)
    return ConnectResponse(
        status="connected",
        message="AppFolio Vendor Portal connected successfully.",
    )


# ---------------------------------------------------------------------------
# ServiceTitan - web-based credential connect
# ---------------------------------------------------------------------------


class ServiceTitanConnectRequest(BaseModel):
    """Request body for connecting ServiceTitan via the web UI.

    The user enters the Tenant ID, Client ID, and Client Secret from
    ServiceTitan's Settings -> Integrations -> API Application Access page.
    """

    tenant_id: str = Field(description="ServiceTitan Tenant ID")
    client_id: str = Field(description="ServiceTitan Client ID")
    client_secret: str = Field(description="ServiceTitan Client Secret")


@router.post("/integrations/servicetitan/connect")
async def servicetitan_web_connect(
    body: ServiceTitanConnectRequest,
    current_user: User = Depends(get_current_user),
) -> ConnectResponse:
    """Connect ServiceTitan via credentials entered in the web UI.

    Validates the three credential values by minting a bearer token
    against the ServiceTitan token endpoint, then persists everything
    so the agent's ServiceTitan tools become available.
    """
    tenant_id = body.tenant_id.strip()
    client_id = body.client_id.strip()
    client_secret = body.client_secret.strip()

    if not tenant_id or not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Tenant ID, Client ID, and Client Secret are all required.",
        )

    if not settings.servicetitan_app_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "ServiceTitan is not configured: the deployment is missing"
                " an App Key. Ask the operator to set SERVICETITAN_APP_KEY."
            ),
        )

    try:
        access_token, expires_at = await mint_access_token(
            client_id=client_id,
            client_secret=client_secret,
        )
    except ServiceTitanAuthError as exc:
        logger.warning("ServiceTitan connect failed for user=%s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=400,
            detail=f"ServiceTitan rejected the credentials: {exc}",
        ) from exc

    await save_credentials(
        current_user.id,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        app_key=settings.servicetitan_app_key,
        access_token=access_token,
        expires_at=expires_at,
    )

    logger.info("User %s connected ServiceTitan via web UI", current_user.id)
    return ConnectResponse(
        status="connected",
        message="ServiceTitan connected successfully.",
    )


@router.delete("/integrations/appfolio_vendor/disconnect")
async def appfolio_web_disconnect(
    current_user: User = Depends(get_current_user),
) -> ConnectResponse:
    """Disconnect AppFolio Vendor Portal by removing stored credentials."""
    from backend.app.integrations.appfolio_vendor.auth import clear_credential

    await clear_credential(current_user.id)
    logger.info("User %s disconnected AppFolio Vendor Portal via web UI", current_user.id)
    return ConnectResponse(
        status="disconnected",
        message="AppFolio Vendor Portal disconnected.",
    )


@router.delete("/integrations/servicetitan/disconnect")
async def servicetitan_web_disconnect(
    current_user: User = Depends(get_current_user),
) -> ConnectResponse:
    """Disconnect ServiceTitan by removing stored credentials."""
    from backend.app.integrations.servicetitan.auth import clear_credentials

    await clear_credentials(current_user.id)
    logger.info("User %s disconnected ServiceTitan via web UI", current_user.id)
    return ConnectResponse(
        status="disconnected",
        message="ServiceTitan disconnected.",
    )
