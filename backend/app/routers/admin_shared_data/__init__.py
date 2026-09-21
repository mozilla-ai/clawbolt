"""Consent-gated access to real user content.

Every route here reads a user's own conversations, memory, or approval
history, so all of them pass through the consent gate in ``consent`` and
redact PII at serialization, after any comparison has been made.

One module per view. They mount under /admin/shared-data in the order the
endpoints were originally declared, so path resolution is unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.routers.admin_shared_data import (
    approvals,
    compaction,
    conversations,
    export,
    profile,
    summary,
)

router = APIRouter(prefix="/admin/shared-data", tags=["admin"])

for _sub in (summary, conversations, profile, compaction, approvals, export):
    router.include_router(_sub.router)

__all__ = ["router"]
