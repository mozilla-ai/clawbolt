"""Google OAuth code exchange, token extraction, and user creation."""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.file_store import UserData
from backend.app.agent.user_db import provision_user
from backend.app.billing.quota import get_current_quota
from backend.app.config import settings
from backend.app.database import db_session_async as oss_db_session_async
from backend.app.models import AllowedEmail, Subscription
from backend.app.models import User as OssUser

logger = logging.getLogger(__name__)


class RegistrationNotAllowed(Exception):
    """Raised when a new user tries to register but is not on the allowlist."""


class AccountDeactivated(Exception):
    """Raised when an existing but deactivated user tries to sign in."""


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _default_redirect_uri() -> str:
    """Build the OAuth callback URL from ``APP_BASE_URL``.

    Mirrors ``backend.app.routers.google_oauth._get_redirect_uri`` so the
    router and the token-exchange call here use the SAME ``redirect_uri``
    string. Google rejects the token exchange with a 400 if the URI
    differs by even a trailing slash from the one sent to the auth
    endpoint, so the two have to share a single source of truth.

    Defined as a module-level helper rather than a const so it picks up
    settings overrides at call time (tests + multi-tenant deploys).
    """
    return f"{settings.app_base_url.rstrip('/')}/api/auth/oauth/google/callback"


async def exchange_google_code(code: str, redirect_uri: str | None = None) -> dict:
    """Exchange authorization code for tokens, return user info from Google.

    *redirect_uri* MUST equal the value the original authorization
    redirect sent to Google. The router computes that URI from
    ``APP_BASE_URL`` (see ``oauth._get_redirect_uri``); when it calls
    this function it passes the same value back in so they're
    guaranteed to match. When the argument is omitted, this function
    derives the same default from ``APP_BASE_URL``.

    The legacy ``GOOGLE_REDIRECT_URI`` setting still exists for
    backward compat with deployments that may have it set to a
    non-default value, but it's no longer the source of truth for the
    URI sent to Google. ``APP_BASE_URL`` is.
    """
    effective_redirect_uri = redirect_uri or _default_redirect_uri()
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": effective_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()

        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        userinfo_resp.raise_for_status()
        return userinfo_resp.json()


def _normalize_email(raw_email: str) -> str:
    return raw_email.lower().strip()


def _resolve_role(email: str) -> str:
    if settings.admin_email and email == settings.admin_email:
        return "admin"
    return "user"


def _user_to_dto(user: OssUser) -> UserData:
    return UserData(
        id=user.id,
        user_id=user.user_id,
        phone=user.phone,
        soul_text=user.soul_text,
        user_text=user.user_text,
        heartbeat_text=user.heartbeat_text,
        timezone=user.timezone,
        preferred_channel=user.preferred_channel,
        channel_identifier=user.channel_identifier,
        onboarding_complete=user.onboarding_complete,
        is_active=user.is_active,
        heartbeat_opt_in=user.heartbeat_opt_in,
        heartbeat_frequency=user.heartbeat_frequency,
        heartbeat_max_daily=user.heartbeat_max_daily,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def finalize_user_provisioning(user_id: str) -> None:
    """Finish OSS file provisioning after the caller commits."""
    async with oss_db_session_async() as db:
        user = (await db.execute(select(OssUser).where(OssUser.id == user_id))).scalar_one_or_none()
        if user is not None:
            await provision_user(user, db)


async def _ensure_registration_allowed(db: AsyncSession, email: str) -> None:
    if settings.registration_mode != "restricted":
        return
    if settings.admin_email and email == settings.admin_email:
        return
    allowed = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if allowed is None:
        raise RegistrationNotAllowed(f"Email {email} is not approved for registration")


async def _ensure_subscription_and_quota(
    db: AsyncSession,
    *,
    user: OssUser,
    email: str,
    role: str,
) -> None:
    subscription = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    if subscription is None:
        try:
            async with db.begin_nested():
                subscription = Subscription(
                    user_id=user.id,
                    role=role,
                    email=email,
                    plan="free",
                    status="active",
                )
                db.add(subscription)
                await db.flush()
        except IntegrityError:
            subscription = (
                await db.execute(select(Subscription).where(Subscription.user_id == user.id))
            ).scalar_one_or_none()
            if subscription is None:
                raise
    elif email and not subscription.email:
        subscription.email = email
        await db.flush()

    await get_current_quota(db, user.id)


async def _get_or_create_user_in_db(db: AsyncSession, google_user_info: dict) -> UserData:
    google_sub = google_user_info["sub"]
    user_id = f"google_{google_sub}"
    email = _normalize_email(google_user_info.get("email", ""))
    role = _resolve_role(email)

    user = (
        await db.execute(select(OssUser).where(OssUser.user_id == user_id))
    ).scalar_one_or_none()
    if user is None:
        await _ensure_registration_allowed(db, email)
        try:
            async with db.begin_nested():
                user = OssUser(user_id=user_id)
                db.add(user)
                await db.flush()
        except IntegrityError:
            user = (
                await db.execute(select(OssUser).where(OssUser.user_id == user_id))
            ).scalar_one_or_none()
            if user is None:
                raise

    if not user.is_active:
        raise AccountDeactivated(f"Account deactivated: {user_id}")

    try:
        await provision_user(user, db, commit=False)
    except Exception:
        logger.warning(
            "provision_user failed for %s during OAuth login; user is still usable",
            user.id,
            exc_info=True,
        )

    await _ensure_subscription_and_quota(db, user=user, email=email, role=role)
    await db.flush()
    return _user_to_dto(user)


async def get_or_create_user(db: AsyncSession, google_user_info: dict) -> UserData:
    """Look up or create a User from Google OAuth user info.

    Uses user_id = "google_<sub>" to match the OSS user_id scoping pattern.
    Also heals the ``Subscription`` / current-month ``UsageQuota``
    rows so retries after a partial signup do not strand the user. The
    whole flow stays inside the caller's transaction; callers that commit
    successfully should then run ``finalize_user_provisioning()`` to
    restore any on-disk bootstrap files.
    """
    return await _get_or_create_user_in_db(db, google_user_info)
