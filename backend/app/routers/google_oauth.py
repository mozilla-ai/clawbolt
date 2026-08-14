"""Google OAuth endpoints.

Provides the server-side OAuth 2.0 flow:
  GET  /auth/oauth/google           -- redirect to Google's consent screen
  GET  /auth/oauth/google/callback  -- exchange code for tokens, log user in
  GET  /auth/oauth/google/state     -- generate a signed state token (legacy)
  POST /auth/oauth/google           -- exchange code via JSON body (legacy)
"""

import datetime
import hashlib
import hmac
import logging
import secrets
from urllib.parse import quote, urlencode

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.jwt_auth import create_access_token, create_refresh_token
from backend.app.auth.oauth_flow import (
    AccountDeactivated,
    RegistrationNotAllowed,
    exchange_google_code,
    finalize_user_provisioning,
    get_or_create_user,
)
from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.middleware.rate_limit import check_rate_limit
from backend.app.schemas import AuthResponse, GoogleAuthRequest, StateResponse
from backend.app.services.login_tracker import update_last_login

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# Temporary copy used while a parallel deployment is migrating users.
# Replace with a generic deactivation message once the migration window closes.
_DEACTIVATED_LOGIN_MESSAGE = "Your account has moved to clawbolt.ai. Sign in there to access it."

# Enforce the Secure flag on cookies when the deployment uses HTTPS.
_USE_SECURE_COOKIES = settings.app_base_url.startswith("https://")


def _get_redirect_uri() -> str:
    """Build the OAuth callback URL from the configured base URL."""
    return f"{settings.app_base_url.rstrip('/')}/api/auth/oauth/google/callback"


def _get_state_signing_key() -> str:
    """Derive a separate signing key for OAuth state tokens via HMAC.

    Uses HMAC(jwt_secret, "oauth_state") so state tokens cannot be confused
    with access/refresh tokens, even if the type claim check were bypassed.
    """
    return hmac.new(
        settings.jwt_secret.encode(),
        b"oauth_state",
        hashlib.sha256,
    ).hexdigest()


def _create_state_token() -> str:
    """Create a short-lived signed state token for OAuth CSRF protection."""
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "type": "oauth_state",
        "iat": now,
        "exp": now + datetime.timedelta(minutes=settings.oauth_state_expiry_minutes),
    }
    return jwt.encode(payload, _get_state_signing_key(), algorithm=settings.jwt_algorithm)


def _validate_state_token(state: str) -> None:
    """Validate a state token. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(
            state,
            _get_state_signing_key(),
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=400, detail="OAuth state expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc
    if payload.get("type") != "oauth_state":
        raise HTTPException(status_code=400, detail="Invalid OAuth state type")


def _error_redirect(message: str) -> RedirectResponse:
    """Redirect to the login page with an error message in the URL hash.

    The frontend LoginPage reads auth_error from the hash fragment and
    displays it inline, so the error page matches the app's design.
    """
    encoded = quote(message, safe="")
    return RedirectResponse(url=f"/app/login#auth_error={encoded}", status_code=302)


# ---------------------------------------------------------------------------
# Server-side OAuth flow (recommended)
# ---------------------------------------------------------------------------


@router.get("/google", response_class=RedirectResponse)
async def oauth_google_redirect() -> RedirectResponse:
    """Redirect to Google's authorization endpoint to start the OAuth flow."""
    if not settings.google_client_id:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")

    state = _create_state_token()

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _get_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }

    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=1800,
        httponly=True,
        samesite="lax",
        secure=_USE_SECURE_COOKIES,
    )
    return response


@router.get("/google/callback")
async def oauth_google_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    oauth_state: str | None = Cookie(None),
    db: AsyncSession = Depends(get_async_db),
) -> RedirectResponse:
    """Handle the OAuth callback from Google.

    Exchanges the authorization code for user info, creates/gets the user,
    issues a refresh token, and redirects to the app with the token in a
    URL hash fragment (never sent to the server or logged by proxies).
    """
    check_rate_limit(request)

    if error:
        return _error_redirect(f"Google sign-in was cancelled: {error}")

    if not code or not state:
        return _error_redirect("Missing authorization code. Please try signing in again.")

    # CSRF: verify state matches either the cookie or the signed JWT
    if oauth_state:
        if not secrets.compare_digest(state, oauth_state):
            return _error_redirect("Invalid state parameter. Please try signing in again.")
    else:
        # Fall back to JWT validation if no cookie (e.g. cross-site cookie blocked)
        try:
            _validate_state_token(state)
        except HTTPException:
            return _error_redirect("Your login session expired. Please try again.")

    try:
        # Pass the same redirect_uri we used in the auth redirect.
        # Google rejects the token exchange with HTTP 400 if these
        # differ — historically this was the source of "OAuth works
        # locally but breaks on prod" because each side read a
        # different setting (router: APP_BASE_URL, exchange: legacy
        # GOOGLE_REDIRECT_URI). One source of truth now.
        google_user_info = await exchange_google_code(code, redirect_uri=_get_redirect_uri())
    except Exception:
        logger.exception("Google OAuth code exchange failed")
        return _error_redirect("Sign-in failed: could not verify with Google.")

    if "sub" not in google_user_info:
        return _error_redirect("Sign-in failed: invalid Google user info.")

    try:
        user = await get_or_create_user(db, google_user_info)
        await update_last_login(db, user.id)
        await db.commit()
    except AccountDeactivated:
        return _error_redirect(_DEACTIVATED_LOGIN_MESSAGE)
    except RegistrationNotAllowed:
        rejected_email = google_user_info.get("email", "")
        error_msg = quote("Your account has not been approved yet. Contact an admin.", safe="")
        email_param = f"&rejected_email={quote(rejected_email, safe='')}" if rejected_email else ""
        return RedirectResponse(
            url=f"/app/login#auth_error={error_msg}{email_param}", status_code=302
        )
    except Exception:
        logger.exception("User creation/lookup failed during OAuth callback")
        return _error_redirect("An unexpected error occurred during sign-in.")

    try:
        await finalize_user_provisioning(user.id)
    except Exception:
        logger.warning(
            "post-commit provision_user failed for %s during OAuth callback",
            user.id,
            exc_info=True,
        )

    refresh_token = create_refresh_token(user.id)

    # Redirect with the refresh token in a hash fragment.
    # The SPA reads the token from the hash, stores it in localStorage, and
    # cleans the URL. Hash fragments are never sent to the server.
    response = RedirectResponse(url=f"/app#refresh_token={refresh_token}", status_code=302)
    response.delete_cookie("oauth_state", secure=_USE_SECURE_COOKIES)
    return response


# ---------------------------------------------------------------------------
# Legacy endpoints (kept for backward compatibility with existing frontend)
# ---------------------------------------------------------------------------


@router.get("/google/state", response_model=StateResponse)
async def get_oauth_state() -> StateResponse:
    """Generate a signed state parameter for OAuth CSRF protection."""
    return StateResponse(state=_create_state_token())


@router.post("/google/exchange", response_model=AuthResponse)
async def google_oauth_exchange(
    body: GoogleAuthRequest, request: Request, db: AsyncSession = Depends(get_async_db)
) -> AuthResponse:
    """Exchange a Google authorization code for JWT tokens (JSON API).

    Legacy endpoint for clients that handle the OAuth redirect themselves.
    """
    check_rate_limit(request)
    _validate_state_token(body.state)

    try:
        google_user_info = await exchange_google_code(body.code)
    except Exception as exc:
        logger.exception("Google OAuth code exchange failed")
        raise HTTPException(status_code=400, detail="Invalid authorization code") from exc

    if "sub" not in google_user_info:
        raise HTTPException(status_code=400, detail="Invalid Google user info")

    try:
        user = await get_or_create_user(db, google_user_info)
    except AccountDeactivated as exc:
        raise HTTPException(status_code=403, detail=_DEACTIVATED_LOGIN_MESSAGE) from exc
    except RegistrationNotAllowed as exc:
        raise HTTPException(
            status_code=403, detail="Your account has not been approved yet."
        ) from exc
    await update_last_login(db, user.id)
    await db.commit()
    try:
        await finalize_user_provisioning(user.id)
    except Exception:
        logger.warning(
            "post-commit provision_user failed for %s during OAuth exchange",
            user.id,
            exc_info=True,
        )
    return AuthResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        user_id=user.id,
    )
