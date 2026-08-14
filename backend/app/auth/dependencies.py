import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.user_db import provision_user
from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.models import User

logger = logging.getLogger(__name__)

LOCAL_USER_ID = "local@clawbolt.local"

CurrentUserResolver = Callable[[Request], Awaitable[User]]

# Set by ``set_current_user_resolver`` when AUTH_MODE=multi_user. Mirrors
# the module-level setter pattern used by ``set_pipeline_override`` and
# ``set_llm_request_observer``.
_current_user_resolver: CurrentUserResolver | None = None


def set_current_user_resolver(resolver: CurrentUserResolver | None) -> None:
    """Register the resolver that authenticates requests in multi_user mode.

    The resolver receives the request and returns the authenticated
    ``User``, or raises ``HTTPException(401)``. It owns its own database
    access: ``get_current_user`` does not hand it a session, because the
    credential it reads (a header, a cookie) may not require one.

    Pass ``None`` to clear, which tests use to restore the default.
    """
    global _current_user_resolver
    _current_user_resolver = resolver


def get_current_user_resolver() -> CurrentUserResolver | None:
    """Return the registered multi_user resolver, or ``None`` if unset."""
    return _current_user_resolver


def validate_auth_mode() -> None:
    """Reject startup when multi_user is requested with no resolver.

    Called from the lifespan, after plugin import has had its chance to
    register one. Failing here turns a silent authentication gap into a
    boot failure: without this, every request would take the
    ``HTTPException(500)`` branch in ``get_current_user`` and the
    operator would learn about it from user reports.
    """
    if settings.auth_mode == "multi_user" and _current_user_resolver is None:
        raise RuntimeError(
            "AUTH_MODE=multi_user but no current-user resolver is registered. "
            "Load a plugin that calls set_current_user_resolver(), or set "
            "AUTH_MODE=single_user."
        )


async def resolve_single_user(db: AsyncSession) -> User:
    """Return the single user, no auth required.

    In single-tenant mode there should be exactly one user. If Telegram
    (or another channel) already created one, return that user so the
    dashboard sees the same sessions, memory, and stats. Only create a local
    fallback when the store is completely empty.
    """
    user = (await db.execute(select(User))).scalars().first()
    if user:
        return user
    user = User(user_id=LOCAL_USER_ID)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # The user row is committed above so it is visible to a fresh
    # AsyncSession; passing ``db=None`` lets ``provision_user`` open its
    # own ``db_session_async()`` rather than reusing the dependency's
    # session.
    await provision_user(user)
    return user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> User:
    """Resolve the caller according to AUTH_MODE.

    single_user (the default) is unchanged self-hosted behavior. multi_user
    delegates to the registered resolver and never falls back to the
    single-user path: a missing resolver is a misconfiguration, and
    serving the first row in ``users`` to an unauthenticated caller would
    be an authentication bypass rather than a degraded mode.

    ``db`` stays a dependency so the single_user path keeps using the
    request-scoped session it has always used. multi_user therefore
    acquires a pooled session it does not use; that is deliberate for now,
    since preserving the default path matters more than the pool slot, and
    mozilla-ai/clawbolt#1510 can revisit it once the multi-user
    implementation actually lives here.
    """
    if settings.auth_mode == "multi_user":
        resolver = _current_user_resolver
        if resolver is None:
            logger.error(
                "AUTH_MODE=multi_user but no current-user resolver is registered; "
                "refusing the request rather than falling back to single-user auth."
            )
            raise HTTPException(status_code=500, detail="Authentication is not configured")
        return await resolver(request)
    return await resolve_single_user(db)
