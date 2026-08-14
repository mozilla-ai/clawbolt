"""Auth endpoints: refresh tokens."""

import logging

from fastapi import APIRouter, HTTPException, Request

from backend.app.agent.file_store import get_user_store
from backend.app.auth.jwt_auth import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from backend.app.middleware.rate_limit import check_rate_limit
from backend.app.schemas import RefreshRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, request: Request) -> TokenResponse:
    """Exchange a valid refresh token for a new access + refresh token pair."""
    check_rate_limit(request)
    payload = decode_refresh_token(body.refresh_token)
    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    store = get_user_store()
    user = await store.get_by_id(user_id_str)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account deactivated")
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )
