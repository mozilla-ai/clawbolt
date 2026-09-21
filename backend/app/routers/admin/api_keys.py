"""Admin console: mint and revoke admin API keys for CLI auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.models import (
    AdminApiKey,
)
from backend.app.query_helpers import fetch_all, iso, iso_or_none
from backend.app.schemas.admin import (
    AdminApiKeyCreate,
    AdminApiKeyItem,
    AdminApiKeyListResponse,
    AdminApiKeyMintResponse,
)
from backend.app.schemas.common import StatusResponse
from backend.app.services.admin_api_keys import (
    ACTIVE_KEY_CAP_PER_ADMIN,
    TooManyActiveKeysError,
    mint_api_key,
    revoke_api_key,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)

router = APIRouter()


def _to_api_key_item(row: AdminApiKey) -> AdminApiKeyItem:
    return AdminApiKeyItem(
        id=row.id,
        label=row.label or "",
        key_prefix=row.key_prefix or "",
        created_at=iso(row.created_at),
        last_used_at=iso_or_none(row.last_used_at),
        revoked_at=iso_or_none(row.revoked_at),
    )


@router.get("/api-keys", response_model=AdminApiKeyListResponse)
async def list_admin_api_keys(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.LIST_ADMIN_API_KEYS)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminApiKeyListResponse:
    """List the admin's own API keys.

    Includes revoked keys (with ``revoked_at`` populated) so the admin
    can audit their own history. The cleartext token is never
    returned; only the prefix + metadata.
    """
    rows = await fetch_all(
        db,
        select(AdminApiKey)
        .where(AdminApiKey.user_id == ctx.admin_user_id)
        .order_by(AdminApiKey.created_at.desc()),
    )
    return AdminApiKeyListResponse(items=[_to_api_key_item(r) for r in rows])


@router.post("/api-keys", response_model=AdminApiKeyMintResponse)
async def create_admin_api_key(
    body: AdminApiKeyCreate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.CREATE_ADMIN_API_KEY)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminApiKeyMintResponse:
    """Mint a new API key for the calling admin.

    Returns the cleartext token in the response body. The caller must
    persist it: a re-read of the row will only expose the prefix.

    Refuses with 409 when the calling admin already has the per-admin
    cap of active (un-revoked) keys. The actionable response is
    "revoke an old key, then mint again", surfaced in the error
    detail so a CLI client can show it verbatim. Revoked keys do not
    count toward the cap.
    """
    try:
        row, cleartext = await mint_api_key(
            db,
            owner_user_id=ctx.admin_user_id,
            label=body.label,
        )
    except TooManyActiveKeysError as exc:
        ctx.resource_type = "admin_api_key"
        ctx.detail = {
            "label_len": len(body.label or ""),
            "outcome": "rejected_active_key_cap",
            "active_count": exc.active_count,
            "cap": exc.cap,
        }
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {exc.active_count} active API keys "
                f"(cap {exc.cap}). Revoke an old key before minting a new one."
            ),
        ) from exc
    ctx.resource_type = "admin_api_key"
    ctx.resource_id = str(row.id)
    ctx.detail = {"label_len": len(body.label or ""), "active_key_cap": ACTIVE_KEY_CAP_PER_ADMIN}
    return AdminApiKeyMintResponse(
        id=row.id,
        token=cleartext,
        key_prefix=row.key_prefix or "",
        label=row.label or "",
        created_at=iso(row.created_at),
    )


@router.delete("/api-keys/{key_id}", response_model=StatusResponse)
async def revoke_admin_api_key(
    key_id: int,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.REVOKE_ADMIN_API_KEY)),
    db: AsyncSession = Depends(get_async_db),
) -> StatusResponse:
    """Revoke one of the admin's own API keys.

    Idempotent: revoking an already-revoked key returns 200 ok.
    Scoped to the calling admin's own keys; an admin cannot revoke
    another admin's keys through this endpoint (a separate force-
    revoke surface would handle that, with stricter audit).
    """
    ctx.resource_type = "admin_api_key"
    ctx.resource_id = str(key_id)
    if not await revoke_api_key(db, key_id=key_id, owner_user_id=ctx.admin_user_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return StatusResponse(status="ok")
