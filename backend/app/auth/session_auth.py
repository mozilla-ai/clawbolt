"""Bearer-token authentication for ``AUTH_MODE=multi_user``.

Registered as the default current-user resolver by
``auth.dependencies.get_current_user``. Deployments that authenticate
some other way replace it via ``set_current_user_resolver``.
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy import select

from backend.app.auth.jwt_auth import decode_access_token
from backend.app.database import db_session_async
from backend.app.models import User
from backend.app.services.admin_api_keys import authenticate_api_key, is_api_key_token

logger = logging.getLogger(__name__)


async def resolve_multi_user(request: Request) -> User:
    """Decode the Bearer token and return the associated user.

    The same Authorization header carries two token families:

    * ``ck_<...>`` -- admin API keys minted via /api/admin/api-keys.
      Looked up by SHA-256 hash; the owner's admin role is re-checked
      on every request so demoting kills all keys instantly. CLI /
      script auth uses this path.
    * Anything else -- JWT, decoded the usual way. Browser sessions
      use this path.

    Picking on the prefix means a JWT that happens to start with
    ``ck_`` would mis-route, but since JWTs are base64url-encoded
    JSON objects starting with ``{"alg":"...`` (so the first three
    base64 chars are ``eyJ``) that collision is structurally
    impossible.

    Stamps ``request.state.auth_source`` to ``"api_key"`` or
    ``"session"`` so the admin audit dependency can record how the
    caller authenticated without re-parsing the header. Used by
    forensic queries that ask "which actions came in via the CLI?".
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = authorization.removeprefix("Bearer ")

    if is_api_key_token(token):
        user = await authenticate_api_key(token)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")
        request.state.auth_source = "api_key"
        return user

    payload = decode_access_token(token)
    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    async with db_session_async() as db:
        user = (await db.execute(select(User).where(User.id == user_id_str))).scalar_one_or_none()
        if user is not None:
            db.expunge(user)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")
    request.state.auth_source = "session"
    return user
