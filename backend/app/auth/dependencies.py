from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.user_db import provision_user
from backend.app.auth.session_auth import resolve_multi_user
from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.models import User

LOCAL_USER_ID = "local@clawbolt.local"

# Placeholder values that are safe locally and unsafe in a public
# multi_user deployment. Mirrors the defaults declared on ``Settings``.
_INSECURE_JWT_SECRET = "change-me-in-production"
_LOCAL_APP_BASE_URL = "http://localhost:8000"

CurrentUserResolver = Callable[[Request], Awaitable[User]]

# Set by ``set_current_user_resolver`` to replace the built-in multi_user
# resolver. Mirrors the module-level setter pattern used by
# ``set_pipeline_override`` and ``set_llm_request_observer``.
_current_user_resolver: CurrentUserResolver | None = None


def set_current_user_resolver(resolver: CurrentUserResolver | None) -> None:
    """Override the resolver that authenticates requests in multi_user mode.

    multi_user already has a built-in resolver
    (``session_auth.resolve_multi_user``: Bearer JWT or admin API key).
    Register here only to authenticate some other way.

    The resolver receives the request and returns the authenticated
    ``User``, or raises ``HTTPException(401)``. It owns its own database
    access: ``get_current_user`` does not hand it a session, because the
    credential it reads (a header, a cookie) may not require one.

    ``get_current_user`` awaits the resolver directly, so FastAPI's
    dependency injection never runs on it. Declare no ``Depends``,
    ``Header``, or ``Query`` parameters: they keep their sentinel default
    object instead of a value, which fails at attribute access rather
    than returning a 401. Read what you need off ``request``.

    Pass ``None`` to clear, which tests use to restore the built-in.
    """
    global _current_user_resolver
    _current_user_resolver = resolver


def get_current_user_resolver() -> CurrentUserResolver:
    """Return the active multi_user resolver: the override, else the built-in."""
    return _current_user_resolver or resolve_multi_user


def validate_auth_mode() -> None:
    """Reject startup on a multi_user deployment that cannot authenticate.

    Called from the lifespan, so a misconfiguration is a boot failure
    rather than something the operator learns about from user reports.

    A public multi_user deployment still running the placeholder
    ``JWT_SECRET`` lets anyone who knows the default mint a token for any
    account. The localhost default for ``APP_BASE_URL`` is treated as
    local development and exempt.
    """
    if settings.auth_mode != "multi_user":
        return
    if settings.app_base_url != _LOCAL_APP_BASE_URL and settings.jwt_secret == _INSECURE_JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is set to the insecure default. Set a strong JWT_SECRET "
            "environment variable before deploying with AUTH_MODE=multi_user."
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
    delegates to the resolver and never falls back to the single-user
    path: serving the first row in ``users`` to an unauthenticated caller
    would be an authentication bypass rather than a degraded mode.

    ``db`` stays a dependency so the single_user path keeps using the
    request-scoped session it has always used. multi_user therefore
    constructs a session it never touches, which costs an object rather
    than a pool connection, since SQLAlchemy checks a connection out on
    first use.
    """
    if settings.auth_mode == "multi_user":
        return await get_current_user_resolver()(request)
    return await resolve_single_user(db)
