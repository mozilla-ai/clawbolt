"""OAuth endpoints for integration authorization flows."""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from backend.app.agent.file_store import UserData
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import OAuthAuthorizeResponse, OAuthStatusEntry, OAuthStatusResponse
from backend.app.services.oauth import (
    get_oauth_config,
    list_oauth_integrations,
    oauth_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/oauth/status", response_model=OAuthStatusResponse)
async def get_oauth_status(
    current_user: UserData = Depends(get_current_user),
) -> OAuthStatusResponse:
    """Return connection status for all OAuth integrations."""
    entries: list[OAuthStatusEntry] = []
    for name in list_oauth_integrations():
        config = get_oauth_config(name)
        entries.append(
            OAuthStatusEntry(
                integration=name,
                configured=config is not None and config.is_configured,
                connected=oauth_service.is_connected(current_user.id, name),
            )
        )
    return OAuthStatusResponse(integrations=entries)


@router.get("/oauth/{integration}/authorize", response_model=OAuthAuthorizeResponse)
async def get_authorize_url(
    integration: str,
    current_user: UserData = Depends(get_current_user),
) -> OAuthAuthorizeResponse:
    """Generate an authorization URL for the given integration."""
    config = get_oauth_config(integration)
    if config is None or not config.is_configured:
        raise HTTPException(status_code=400, detail=f"Integration not configured: {integration}")

    url = oauth_service.get_authorization_url(config, current_user.id)
    return OAuthAuthorizeResponse(url=url, integration=integration)


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    realmId: str = Query(""),  # QuickBooks sends camelCase
    error: str = Query(""),
    error_description: str = Query(""),
) -> RedirectResponse:
    """Handle OAuth provider redirect after user authorization.

    The provider redirects here with an authorization code and the state
    parameter we generated earlier. We exchange the code for tokens,
    persist them, and redirect the user to the frontend success page.
    """
    if error:
        logger.warning("OAuth callback error: %s - %s", error, error_description)
        msg = urllib.parse.quote(error_description or error)
        return RedirectResponse(
            f"/app/oauth/callback?status=error&error={msg}",
            status_code=302,
        )

    integration = oauth_service.get_pending_state_integration(state)

    try:
        await oauth_service.handle_callback(state, code, realm_id=realmId)
    except ValueError as exc:
        logger.warning("OAuth callback failed: %s", exc)
        msg = urllib.parse.quote(str(exc))
        return RedirectResponse(
            f"/app/oauth/callback?status=error&error={msg}",
            status_code=302,
        )
    except Exception:
        logger.exception("OAuth token exchange failed")
        return RedirectResponse(
            "/app/oauth/callback?status=error&error=Token+exchange+failed",
            status_code=302,
        )

    return RedirectResponse(
        f"/app/oauth/callback?status=success&integration={integration or 'unknown'}",
        status_code=302,
    )


@router.delete("/oauth/{integration}")
async def disconnect_integration(
    integration: str,
    current_user: UserData = Depends(get_current_user),
) -> dict[str, str]:
    """Disconnect an OAuth integration by removing stored tokens."""
    deleted = oauth_service.delete_token(current_user.id, integration)
    if not deleted:
        raise HTTPException(status_code=404, detail="No connection found for this integration")
    return {"status": "disconnected", "integration": integration}
