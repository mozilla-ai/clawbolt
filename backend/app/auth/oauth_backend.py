"""AuthBackend implementation for Google OAuth + JWT."""

from typing import Any

from sqlalchemy import select

from backend.app.auth.base import AuthBackend
from backend.app.auth.jwt_auth import decode_access_token
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.models import User


class OAuthBackend(AuthBackend):
    """Auth backend using Google OAuth for signup and JWT for sessions."""

    def get_auth_config(self) -> dict[str, Any]:
        return {
            "method": "oauth_google",
            "required": True,
            "google_client_id": settings.google_client_id,
        }

    async def authenticate_login(self, credentials: dict[str, str]) -> User:
        """Validate a JWT access token and return the associated user."""
        from fastapi import HTTPException

        token = credentials.get("token", "")
        payload = decode_access_token(token)
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise HTTPException(status_code=401, detail="Invalid token subject")
        async with db_session_async() as db:
            user = (
                await db.execute(select(User).where(User.id == user_id_str))
            ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.is_active:
            raise HTTPException(status_code=401, detail="Account deactivated")
        return user

    async def on_user_created(self, user: User) -> None:
        """Seed subscription and quota for newly created user.

        This is handled in oauth_flow.get_or_create_user, so this is a no-op.
        """
