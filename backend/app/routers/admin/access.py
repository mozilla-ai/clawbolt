"""Admin console: registration gating via the allowlist and the waitlist."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.models import (
    AllowedEmail,
    WaitlistEntry,
)
from backend.app.query_helpers import count_rows, fetch_all, iso
from backend.app.schemas.admin import (
    AllowedEmailCreate,
    AllowedEmailListResponse,
    AllowedEmailResponse,
    DeleteResponse,
    WaitlistEntryResponse,
    WaitlistListResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.email_service import send_waitlist_approved

router = APIRouter()


@router.get("/allowed-emails", response_model=AllowedEmailListResponse)
async def list_allowed_emails(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_ALLOWED_EMAILS)),
    db: AsyncSession = Depends(get_async_db),
) -> AllowedEmailListResponse:
    """List all pre-approved email addresses."""
    rows = await fetch_all(db, select(AllowedEmail).order_by(AllowedEmail.email))
    ctx.detail = {"count": len(rows)}
    return AllowedEmailListResponse(
        total=len(rows),
        items=[
            AllowedEmailResponse(
                id=r.id,
                email=r.email,
                note=r.note,
                created_at=iso(r.created_at),
            )
            for r in rows
        ],
    )


@router.post("/allowed-emails", response_model=AllowedEmailResponse)
async def add_allowed_email(
    body: AllowedEmailCreate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.ADD_ALLOWED_EMAIL)),
    db: AsyncSession = Depends(get_async_db),
) -> AllowedEmailResponse:
    """Add an email address to the approved registration list."""
    normalized = body.email.lower().strip()
    ctx.resource_type = "allowed_email"
    ctx.detail = {"email": normalized}
    existing = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == normalized))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Email already in allowed list")
    entry = AllowedEmail(email=normalized, note=body.note)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    ctx.resource_id = str(entry.id)
    return AllowedEmailResponse(
        id=entry.id,
        email=entry.email,
        note=entry.note,
        created_at=iso(entry.created_at),
    )


@router.delete("/allowed-emails/{email_id}", response_model=DeleteResponse)
async def remove_allowed_email(
    email_id: int,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.REMOVE_ALLOWED_EMAIL)),
    db: AsyncSession = Depends(get_async_db),
) -> DeleteResponse:
    """Remove an email address from the approved registration list."""
    ctx.resource_type = "allowed_email"
    ctx.resource_id = str(email_id)
    entry = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.id == email_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Allowed email not found")
    ctx.detail = {"email": entry.email}
    await db.delete(entry)
    await db.commit()
    return DeleteResponse(deleted=True, id=email_id)


@router.get("/waitlist", response_model=WaitlistListResponse)
async def list_waitlist_entries(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_WAITLIST)),
    db: AsyncSession = Depends(get_async_db),
) -> WaitlistListResponse:
    """List waitlist entries, newest first."""
    ctx.detail = {"offset": offset, "limit": limit}
    total = await count_rows(db, WaitlistEntry.id)
    rows = await fetch_all(
        db,
        select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc()).offset(offset).limit(limit),
    )
    return WaitlistListResponse(
        total=total,
        items=[
            WaitlistEntryResponse(
                id=r.id,
                email=r.email,
                name=r.name,
                use_case=r.use_case,
                source=r.source,
                created_at=iso(r.created_at),
            )
            for r in rows
        ],
    )


@router.post("/waitlist/{entry_id}/approve", response_model=AllowedEmailResponse)
async def approve_waitlist_entry(
    entry_id: int,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.APPROVE_WAITLIST)),
    db: AsyncSession = Depends(get_async_db),
) -> AllowedEmailResponse:
    """Approve a waitlist entry: add to allowed_emails and remove from waitlist."""
    ctx.resource_type = "waitlist_entry"
    ctx.resource_id = str(entry_id)
    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")
    ctx.detail = {"email": entry.email}

    # Add to allowed_emails if not already there
    existing = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == entry.email))
    ).scalar_one_or_none()
    if existing is None:
        allowed = AllowedEmail(email=entry.email, note="Approved from waitlist")
        db.add(allowed)
        await db.flush()
    else:
        allowed = existing

    approved_email = entry.email
    approved_name = entry.name
    # Captured before the row is deleted so the audit log preserves the
    # context the operator saw when they hit Approve. The waitlist row
    # itself is gone after this commit.
    approved_use_case = entry.use_case
    await db.delete(entry)
    await db.commit()
    await db.refresh(allowed)

    # Best-effort approval email. The DB write above is the source of truth;
    # SES outages must not undo the approval or surface as a 500 to the admin.
    email_sent = await send_waitlist_approved(approved_email, approved_name)
    ctx.detail = {
        "email": approved_email,
        "name": approved_name,
        "use_case": approved_use_case,
        "approval_email_sent": email_sent,
    }

    return AllowedEmailResponse(
        id=allowed.id,
        email=allowed.email,
        note=allowed.note,
        created_at=iso(allowed.created_at),
    )


@router.delete("/waitlist/{entry_id}", response_model=DeleteResponse)
async def dismiss_waitlist_entry(
    entry_id: int,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DISMISS_WAITLIST)),
    db: AsyncSession = Depends(get_async_db),
) -> DeleteResponse:
    """Remove a waitlist entry without approving."""
    entry = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")
    await db.delete(entry)
    await db.commit()
    return DeleteResponse(deleted=True, id=entry_id)
