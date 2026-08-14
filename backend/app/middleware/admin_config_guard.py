"""Admin-only guard for model, channel, and system-prompt endpoints.

In multi-user mode, only admins may change server-level model and channel
settings.
Regular users interact with the platform config set by the admin. The
conversation system prompt preview is also admin-only, since it exposes the
operator's preamble and tool wiring. File storage is per-user (Google Drive
OAuth) and not gated here.

Implemented as pure ASGI middleware to avoid BaseHTTPMiddleware issues
with streaming responses.
"""

import json
import logging

from sqlalchemy import select
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.auth.jwt_auth import decode_access_token
from backend.app.database import db_session_async
from backend.app.models import Subscription

logger = logging.getLogger(__name__)

# Paths that only admins may write to.
_PUT_PROTECTED_PATHS: set[str] = {
    "/api/user/model/config",
    "/api/user/channels/config",
}

# Paths that only admins may read. The model config exposes the LLM
# provider/model the platform is using; in a multi-tenant deployment we treat
# that as an admin-only operational detail, not a per-tenant setting. The
# conversation system prompt reveals the operator's preamble, tool guidance,
# and integration wiring, so it sits behind the same admin gate.
_GET_PROTECTED_PATHS: set[str] = {
    "/api/user/model/config",
    "/api/user/conversation/system-prompt",
}

_FORBIDDEN_BODY = json.dumps(
    {"detail": "Admin access required to view or modify platform settings"}
).encode()


class AdminConfigGuardMiddleware:
    """Block non-admin users from accessing platform-level config (pure ASGI)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")

        is_protected = (method == "PUT" and path in _PUT_PROTECTED_PATHS) or (
            method == "GET" and path in _GET_PROTECTED_PATHS
        )
        if not is_protected:
            await self.app(scope, receive, send)
            return

        # Extract Bearer token from headers
        token: str | None = None
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header_val = value.decode("latin-1")
                if header_val.startswith("Bearer "):
                    token = header_val.removeprefix("Bearer ")
                break

        if not token:
            # No token: let the auth dependency handle the 401
            await self.app(scope, receive, send)
            return

        if not await _is_admin(token):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_FORBIDDEN_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})
            return

        await self.app(scope, receive, send)


async def _is_admin(token: str) -> bool:
    """Decode the JWT and check if the user has admin role.

    Admin is granted exclusively by ``Subscription.role == "admin"``. The
    legacy ``ADMIN_USER_IDS`` env-var fallback was retired so that admin
    grants live in one place; see ``backend.app.auth.admin_dep`` for
    the request-time dependency that enforces the same rule.
    """
    try:
        payload = decode_access_token(token)
    except Exception:
        return False

    user_id = payload.get("sub")
    if not user_id:
        return False

    async with db_session_async() as db:
        sub = (
            await db.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).scalar_one_or_none()
        if sub and sub.role == "admin":
            return True

    return False
