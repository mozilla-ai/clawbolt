"""Deployment-level feature flags consumed by the frontend.

A single GET endpoint that returns the set of runtime flags the React app
needs to know about (e.g. whether the chat page should expose the file
attachment affordance). The values come from ``Settings`` so a deployment
can flip them via env vars without a rebuild. No auth required: the
payload contains nothing sensitive and the frontend fetches it before
the user is signed in.
"""

from fastapi import APIRouter

from backend.app.config import settings
from backend.app.schemas import AppConfigResponse

router = APIRouter()


@router.get("/app/config", response_model=AppConfigResponse)
async def app_config() -> AppConfigResponse:
    return AppConfigResponse(
        chat_web_attachments_enabled=settings.chat_web_attachments_enabled,
    )
