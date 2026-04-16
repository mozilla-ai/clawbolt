"""Temporary media serving endpoint for external integrations.

Serves staged media bytes via a short-lived, token-protected URL so that
external services (like CompanyCam) can download images that Clawbolt has
in memory. Tokens expire after 5 minutes.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 300  # 5 minutes
_store: dict[str, tuple[bytes, str, float]] = {}  # token -> (bytes, mime_type, expires_at)

router = APIRouter()


def create_temp_media_url(
    file_bytes: bytes,
    mime_type: str,
    base_url: str,
) -> str:
    """Stage bytes and return a publicly accessible URL.

    The URL is valid for 5 minutes. CompanyCam (or any external service)
    can GET this URL to download the image.
    """
    _cleanup()
    token = secrets.token_urlsafe(32)
    _store[token] = (file_bytes, mime_type, time.time() + _TOKEN_TTL_SECONDS)
    return f"{base_url.rstrip('/')}/api/media/temp/{token}"


@router.get("/media/temp/{token}")
async def serve_temp_media(token: str, request: Request) -> Response:
    """Serve a temporarily staged media file. No auth required.

    The token stays valid for the full TTL (5 minutes) and can be
    fetched multiple times. External services like CompanyCam may
    download the image more than once (original + thumbnails).
    """
    _cleanup()
    entry = _store.get(token)
    user_agent = request.headers.get("user-agent", "unknown")
    if entry is None:
        logger.warning(
            "Temp media not found: token=%s...%s ua=%s", token[:8], token[-4:], user_agent
        )
        raise HTTPException(status_code=404, detail="Media not found or expired")
    file_bytes, mime_type, expires_at = entry
    if time.time() > expires_at:
        del _store[token]
        logger.warning("Temp media expired: token=%s...%s", token[:8], token[-4:])
        raise HTTPException(status_code=410, detail="Media expired")
    logger.info(
        "Temp media served: token=%s...%s size=%d mime=%s ua=%s",
        token[:8],
        token[-4:],
        len(file_bytes),
        mime_type,
        user_agent,
    )
    return Response(content=file_bytes, media_type=mime_type)


def _cleanup() -> None:
    """Remove expired entries."""
    now = time.time()
    expired = [k for k, (_, _, exp) in _store.items() if now > exp]
    for k in expired:
        del _store[k]
