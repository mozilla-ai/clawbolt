"""OAuth endpoints for integration authorization flows."""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.app.auth.dependencies import get_current_user
from backend.app.models import User
from backend.app.schemas import (
    OAuthAuthorizeResponse,
    OAuthStatusEntry,
    OAuthStatusResponse,
    TokenConnectRequest,
    TokenConnectResponse,
)
from backend.app.services.oauth import (
    OAuthTokenData,
    get_oauth_config,
    list_oauth_integrations,
    oauth_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_TOKEN_BASED_INTEGRATIONS = ("companycam",)


@router.get("/oauth/status", response_model=OAuthStatusResponse)
async def get_oauth_status(
    current_user: User = Depends(get_current_user),
) -> OAuthStatusResponse:
    """Return connection status for all OAuth and token-based integrations."""
    from backend.app.config import settings

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
    for name in _TOKEN_BASED_INTEGRATIONS:
        has_user_token = oauth_service.is_connected(current_user.id, name)
        has_env_token = bool(getattr(settings, f"{name}_access_token", ""))
        entries.append(
            OAuthStatusEntry(
                integration=name,
                configured=True,
                connected=has_user_token or has_env_token,
            )
        )
    return OAuthStatusResponse(integrations=entries)


@router.get("/oauth/{integration}/authorize", response_model=OAuthAuthorizeResponse)
async def get_authorize_url(
    integration: str,
    current_user: User = Depends(get_current_user),
) -> OAuthAuthorizeResponse:
    """Generate an authorization URL for the given integration."""
    config = get_oauth_config(integration)
    if config is None or not config.is_configured:
        raise HTTPException(status_code=400, detail=f"Integration not configured: {integration}")

    url = oauth_service.get_authorization_url(config, current_user.id)
    return OAuthAuthorizeResponse(url=url, integration=integration)


@router.get("/oauth/callback", response_model=None)
async def oauth_callback(
    code: str = Query(""),
    state: str = Query(""),
    realmId: str = Query(""),  # QuickBooks sends camelCase
    error: str = Query(""),
    error_description: str = Query(""),
) -> RedirectResponse | HTMLResponse:
    """Handle OAuth provider redirect after user authorization.

    The provider redirects here with an authorization code and the state
    parameter we generated earlier. We exchange the code for tokens,
    persist them, and redirect the user to the frontend success page.

    All parameters are optional because OAuth providers may redirect with
    only error parameters (no code/state) when the user denies access.

    When the flow was initiated from chat (source="chat"), a standalone
    HTML page is returned instead of redirecting to the SPA, so users
    on SMS/iMessage see a "you can close this tab" message.
    """
    source = oauth_service.get_pending_state_source(state) if state else "web"

    if error:
        logger.warning("OAuth callback error: %s - %s", error, error_description)
        msg = error_description or error
        if source == "chat":
            return _chat_callback_page(success=False, error_message=msg)
        return RedirectResponse(
            f"/app/oauth/callback?status=error&error={urllib.parse.quote(msg)}",
            status_code=302,
        )

    if not code or not state:
        logger.warning("OAuth callback missing code or state: code=%r state=%r", code, state)
        msg = "Missing authorization code"
        if source == "chat":
            return _chat_callback_page(success=False, error_message=msg)
        return RedirectResponse(
            f"/app/oauth/callback?status=error&error={urllib.parse.quote(msg)}",
            status_code=302,
        )

    integration = oauth_service.get_pending_state_integration(state)

    try:
        await oauth_service.handle_callback(state, code, realm_id=realmId)
    except ValueError as exc:
        logger.warning("OAuth callback failed: %s", exc)
        if source == "chat":
            return _chat_callback_page(success=False, error_message=str(exc))
        return RedirectResponse(
            f"/app/oauth/callback?status=error&error={urllib.parse.quote(str(exc))}",
            status_code=302,
        )
    except Exception:
        logger.exception("OAuth token exchange failed")
        if source == "chat":
            return _chat_callback_page(success=False, error_message="Token exchange failed")
        return RedirectResponse(
            "/app/oauth/callback?status=error&error=Token+exchange+failed",
            status_code=302,
        )

    if source == "chat":
        return _chat_callback_page(success=True, integration=integration or "unknown")

    return RedirectResponse(
        f"/app/oauth/callback?status=success&integration={integration or 'unknown'}",
        status_code=302,
    )


def _chat_callback_page(
    *,
    success: bool,
    integration: str = "",
    error_message: str = "",
) -> HTMLResponse:
    """Render a standalone HTML page for chat-initiated OAuth callbacks.

    This page is self-contained (no SPA, no auth required) so it works
    when users tap an OAuth link from SMS/iMessage and complete the flow
    in their phone's browser.
    """
    if success:
        title = "Connected"
        icon = "&#10003;"
        icon_color = "#17c964"
        body = (
            f"<p>{integration.replace('_', ' ').title()} has been connected successfully.</p>"
            "<p>You can close this tab and go back to your chat.</p>"
        )
    else:
        title = "Connection Failed"
        icon = "&#10007;"
        icon_color = "#f31260"
        safe_error = error_message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = (
            f"<p>{safe_error or 'Something went wrong. Please try again.'}</p>"
            "<p>Go back to your chat and ask to try connecting again.</p>"
        )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Clawbolt</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100dvh;
    margin: 0;
    background: #fafafa;
    color: #333;
  }}
  .card {{
    text-align: center;
    max-width: 360px;
    padding: 2.5rem 2rem;
    background: #fff;
    border-radius: 1rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }}
  .icon {{
    font-size: 3rem;
    color: {icon_color};
    margin-bottom: 0.75rem;
  }}
  h1 {{
    font-size: 1.25rem;
    margin: 0 0 1rem;
  }}
  p {{
    font-size: 0.9rem;
    color: #666;
    margin: 0.5rem 0;
    line-height: 1.5;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  {body}
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/oauth/{integration}/token", response_model=TokenConnectResponse)
async def connect_with_token(
    integration: str,
    body: TokenConnectRequest,
    current_user: User = Depends(get_current_user),
) -> TokenConnectResponse:
    """Connect a token-based integration by validating and storing an API token."""
    if integration not in _TOKEN_BASED_INTEGRATIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Integration '{integration}' does not support token-based auth",
        )

    if integration == "companycam":
        from backend.app.services.companycam import CompanyCamService

        service = CompanyCamService(access_token=body.token)
        try:
            user_info = await service.validate_token()
        except Exception as exc:
            logger.warning("CompanyCam token validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid CompanyCam API token") from exc

        token_data = OAuthTokenData(access_token=body.token, token_type="Bearer")
        oauth_service.save_token(current_user.id, integration, token_data)

        display_name = user_info.first_name or ""
        logger.info("User %s connected CompanyCam via API token", current_user.id)
        return TokenConnectResponse(
            status="connected",
            integration=integration,
            display_name=display_name,
        )

    raise HTTPException(status_code=400, detail=f"Unsupported integration: {integration}")


@router.delete("/oauth/{integration}")
async def disconnect_integration(
    integration: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Disconnect an OAuth or token-based integration by removing stored tokens."""
    deleted = oauth_service.delete_token(current_user.id, integration)
    if not deleted:
        raise HTTPException(status_code=404, detail="No connection found for this integration")
    return {"status": "disconnected", "integration": integration}
