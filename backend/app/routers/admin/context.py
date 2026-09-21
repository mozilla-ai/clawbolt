"""Admin console: clear a poisoned conversation context without dropping memory."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.context import admin_compact_visible_messages, hygiene_compact_memory
from backend.app.agent.user_db import get_user_store
from backend.app.auth.admin_dep import get_current_admin
from backend.app.models import (
    User,
)
from backend.app.schemas.admin import (
    CompactUserContextRequest,
    CompactUserContextResponse,
    HygieneCompactMemoryResponse,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)

router = APIRouter()


@router.post(
    "/users/{user_id}/compact-now",
    response_model=CompactUserContextResponse,
)
async def compact_user_context(
    user_id: str,
    body: CompactUserContextRequest,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.COMPACT_USER_CONTEXT)),
    admin: User = Depends(get_current_admin),
) -> CompactUserContextResponse:
    """Synchronously compact a user's currently-visible conversation context.

    Use this when a bug or model error has poisoned a user's in-context
    conversation history (e.g. the agent confidently asserted a wrong
    fact about its own capabilities) and you want to reset the LLM-facing
    context without dropping durable user-supplied facts. The OSS
    ``admin_compact_visible_messages`` helper extracts facts into
    MEMORY.md / USER.md / SOUL.md before advancing the trim watermark,
    so the next turn starts from a clean slate plus the rewritten memory.

    ``keep_recent`` preserves the last N visible turns so the user's
    pending request is not lost when an admin clears stale context
    mid-conversation. ``hint`` is prepended to the compaction LLM's
    ``<conversation>`` block as ``[admin note: ...]`` to bias how the
    LLM reads the messages, which is useful when the exact failure mode
    is known (e.g. "ignore prior agent claims about being read-only").

    Audit-logged via ``AdminAction.COMPACT_USER_CONTEXT``; the resulting
    ``compaction_events`` row is also linked from ``ctx.detail`` so a
    forensic query can join admin action to the compaction outcome.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id
    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot compact their own context")
    ctx.target_user_id = user_id
    ctx.detail = {"keep_recent": body.keep_recent, "hint_provided": bool(body.hint)}

    result = await admin_compact_visible_messages(
        user_id,
        keep_recent=body.keep_recent,
        admin_note=body.hint,
    )

    # Surface the outcome in the audit detail so a downstream review can
    # see what actually happened (e.g. zero compacted = no-op call) without
    # joining to ``compaction_events``.
    ctx.detail = {
        **ctx.detail,
        "compacted_message_count": result.compacted_message_count,
        "new_watermark": result.new_watermark,
        "memory_updated": result.memory_updated,
        "event_id": result.event_id,
        "previous_event_id": result.previous_event_id,
    }

    return CompactUserContextResponse(
        compacted_message_count=result.compacted_message_count,
        new_watermark=result.new_watermark,
        memory_updated=result.memory_updated,
        event_id=result.event_id,
        previous_event_id=result.previous_event_id,
    )


@router.post(
    "/users/{user_id}/hygiene-compact-memory",
    response_model=HygieneCompactMemoryResponse,
)
async def hygiene_compact_memory_endpoint(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.HYGIENE_COMPACT_MEMORY)),
    admin: User = Depends(get_current_admin),
) -> HygieneCompactMemoryResponse:
    """Re-audit a user's MEMORY.md against the Do-Not-Include list.

    Runs the compaction LLM in hygiene-only mode: the model reads the
    user's current MEMORY.md and removes every line that violates the
    exclusion list (customer IDs, phone numbers, stale bug notes, etc.),
    even if no new conversation triggered the compaction. This is the
    "clean my memory now" operation that scrubs pre-existing violations
    that were written before the compliance rule existed.

    Unlike ``POST /admin/users/{user_id}/compact-now``, this endpoint
    does not require untrimmed conversation messages and does not
    advance the trim watermark. It only touches MEMORY.md.

    Audit-logged via ``AdminAction.HYGIENE_COMPACT_MEMORY``.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id
    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(
            status_code=400, detail="Admins cannot hygiene-compact their own memory"
        )
    ctx.target_user_id = user_id

    memory_text, changed = await hygiene_compact_memory(user_id)

    ctx.detail = {
        "memory_updated": changed,
        "memory_bytes": len(memory_text.encode("utf-8")) if memory_text else 0,
    }

    return HygieneCompactMemoryResponse(
        memory_updated=changed,
        memory_text=memory_text,
    )
