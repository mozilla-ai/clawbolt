"""Admin audit log service and FastAPI dependency.

Wraps every admin endpoint with one call to ``Depends(audit_admin(action))``,
which writes a structured row to ``admin_audit_logs`` when the route exits
(either normally or with an exception, so 404 attempts are still recorded).

The audit write is best-effort: failures log a warning but do not block
the route's response. The dependency runs the insert in a fresh
``AsyncSession`` bound directly to the engine, so a failure (FK
violation, transient DB issue) cannot roll back state the route's
session needs.

A reliable fail-closed-on-mutation contract isn't implementable as a
yield-dependency cleanup. Raising ``HTTPException`` there either shadows
a route-raised 404 or trips Starlette's "response already started"
RuntimeError. If we ever want fail-closed for mutations, it'll need a
route decorator that controls response building.

Replaces the inline ``_write_admin_audit()`` helper from #324. Both
write to the same table; rows from before p022 carry only ``endpoint``,
rows from this dependency also carry ``action`` / ``resource_type`` /
``resource_id`` / ``detail`` / ``admin_email``.

Adding a new admin endpoint:

1. Add a member to ``AdminAction``.
2. Decorate the route:
   ``ctx: AdminAuditContext = Depends(audit_admin(AdminAction.YOUR_ACTION))``.
3. Inside the handler, populate ``ctx.target_user_id`` (after the
   existence check, since the column is FK-constrained),
   ``ctx.resource_type`` / ``ctx.resource_id``, and ``ctx.detail`` as
   appropriate.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.admin_dep import get_current_admin
from backend.app.database import get_async_db, get_async_engine
from backend.app.models import AdminAuditLog, Subscription, User

logger = logging.getLogger(__name__)


class AdminAction(StrEnum):
    """Stable identifiers for admin endpoints, written to ``audit.action``."""

    VIEW_USER_LIST = "view_user_list"
    VIEW_USER_DETAIL = "view_user_detail"
    VIEW_HEARTBEAT_LOGS = "view_heartbeat_logs"
    VIEW_LLM_USAGE_LOGS = "view_llm_usage_logs"
    VIEW_USAGE = "view_usage"
    VIEW_STATS = "view_stats"
    VIEW_ALLOWED_EMAILS = "view_allowed_emails"
    VIEW_WAITLIST = "view_waitlist"
    VIEW_CHANNEL_CONFIG = "view_channel_config"

    ACTIVATE_USER = "activate_user"
    DEACTIVATE_USER = "deactivate_user"
    DELETE_USER = "delete_user"
    RESET_QUOTA = "reset_quota"
    UPDATE_USER_PLAN = "update_user_plan"
    COMPACT_USER_CONTEXT = "compact_user_context"
    HYGIENE_COMPACT_MEMORY = "hygiene_compact_memory"

    ADD_ALLOWED_EMAIL = "add_allowed_email"
    REMOVE_ALLOWED_EMAIL = "remove_allowed_email"

    APPROVE_WAITLIST = "approve_waitlist"
    DISMISS_WAITLIST = "dismiss_waitlist"

    UPDATE_CHANNEL_CONFIG = "update_channel_config"
    SET_TELEGRAM_WEBHOOK = "set_telegram_webhook"
    DELETE_TELEGRAM_WEBHOOK = "delete_telegram_webhook"

    VIEW_LLM_CONFIG = "view_llm_config"
    UPDATE_LLM_CONFIG = "update_llm_config"
    VIEW_USER_LLM_OVERRIDE = "view_user_llm_override"
    UPDATE_USER_LLM_OVERRIDE = "update_user_llm_override"
    VIEW_LLM_PROVIDERS = "view_llm_providers"
    VIEW_LLM_PROVIDER_MODELS = "view_llm_provider_models"
    # Download the captured LLM request payloads for one user (current
    # and previous era). Consent-gated: only users with
    # ``data_sharing_consent`` are captured in the first place, so a 404
    # is the natural response for non-consenting users.
    EXPORT_LLM_PAYLOADS = "export_llm_payloads"

    # Consent-gated content access (issue #325 item 3). These are the
    # only routes that surface message bodies / memory text to admins;
    # they're filtered to users who set ``data_sharing_consent=True``
    # and PII-redacted before serialization. The audit row captures
    # which conversation / which user, so a forensic query can answer
    # "which admin read which consenting user's content, and when?"
    VIEW_SHARED_DATA_SUMMARY = "view_shared_data_summary"
    VIEW_SHARED_DATA_USERS = "view_shared_data_users"
    VIEW_SHARED_DATA_CONVERSATIONS = "view_shared_data_conversations"
    # ``view_shared_data_messages`` (the flat-list endpoint) was retired
    # in #361's follow-up. Older audit rows may carry the string; new
    # writes go to VIEW_SHARED_DATA_CONVERSATION_TURNS instead.
    VIEW_SHARED_DATA_CONVERSATION_TURNS = "view_shared_data_conversation_turns"
    VIEW_SHARED_DATA_PROFILE = "view_shared_data_profile"
    VIEW_SHARED_DATA_HEARTBEAT_LOGS = "view_shared_data_heartbeat_logs"
    VIEW_SHARED_DATA_MEMORY = "view_shared_data_memory"
    VIEW_SHARED_DATA_COMPACTION_EVENTS = "view_shared_data_compaction_events"
    VIEW_SHARED_DATA_APPROVAL_EVENTS = "view_shared_data_approval_events"
    # Composite export endpoint: bundles every consent-gated surface
    # for one user into a single response. Cheaper to audit-log as one
    # row with the include flag in detail than to spread across the
    # per-surface actions.
    VIEW_SHARED_DATA_EXPORT = "view_shared_data_export"

    # Admin API keys (long-lived bearer tokens for CLI auth). All three
    # mutations are audited so a forensic query can reconstruct who
    # minted which key and when.
    LIST_ADMIN_API_KEYS = "list_admin_api_keys"
    CREATE_ADMIN_API_KEY = "create_admin_api_key"
    REVOKE_ADMIN_API_KEY = "revoke_admin_api_key"

    # Diagnostic surfaces for the photo pipeline. Both tables are
    # unconditionally checked-into-DB plumbing; surfacing them via the
    # admin API removes the "ssh into prod and run psql" step from
    # contractor-lost-photo investigations.
    VIEW_STAGED_MEDIA = "view_staged_media"
    VIEW_WEBHOOK_EVENTS = "view_webhook_events"

    # User-initiated reports (issue #325 item 5). When a user texts
    # ``/report`` through any channel, OSS writes a ReportedConversation
    # row; these endpoints let admins triage the queue. The list view
    # is metadata-only; the messages view is content (PII-redacted).
    # Dismissing a report stamps the closing admin in the row itself,
    # but we audit-log it as well to keep the cross-table forensic
    # trail consistent with every other admin mutation.
    VIEW_REPORTED_CONVERSATIONS = "view_reported_conversations"
    VIEW_REPORTED_CONVERSATION_MESSAGES = "view_reported_conversation_messages"
    DISMISS_REPORTED_CONVERSATION = "dismiss_reported_conversation"

    # Out-of-band actions (CLI, jobs). Not bound to a request, so the
    # ``audit_admin`` dependency doesn't write these; call sites build
    # the row directly and stamp ``admin_user_id=NULL`` because there's
    # no authenticated admin behind a one-shot operator command.
    PROMOTE_ENV_ADMIN = "promote_env_admin"


@dataclass
class AdminAuditContext:
    """Mutable audit context yielded into a route.

    The route sets ``target_user_id``, ``resource_id``, and ``detail``
    fields before returning. The dependency commits one row when the
    route exits, normally or with an exception.

    ``auth_source`` records which auth path the calling admin came in
    on: ``"session"`` for the JWT path (browser) or ``"api_key"`` for
    a ``ck_<...>`` admin API key (CLI / curl). The dep populates it
    from ``request.state.auth_source``; routes do not set it. The
    field is merged into the persisted ``detail`` JSON at commit time
    so a forensic query like ``detail->>'auth_source' = 'api_key'``
    can answer "which actions came in via the CLI this week?".
    """

    admin_user_id: str
    action: AdminAction
    endpoint: str  # "METHOD /path/{template}" -- populated from the request
    admin_email: str | None = None
    target_user_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    detail: dict | None = None
    auth_source: str = "session"


def audit_admin(
    action: AdminAction,
) -> Callable[..., AsyncGenerator[AdminAuditContext, None]]:
    """FastAPI dependency factory.

    Usage::

        @router.get("/users")
        async def list_users(
            ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USER_LIST)),
            db: AsyncSession = Depends(get_async_db),
        ) -> UserListResponse:
            ...

    The returned dependency also resolves ``get_current_admin`` so the
    route gets admin auth + audit context in one declaration.
    """

    async def dep(
        request: Request,
        admin: User = Depends(get_current_admin),
        db: AsyncSession = Depends(get_async_db),
    ) -> AsyncGenerator[AdminAuditContext, None]:
        ctx = AdminAuditContext(
            admin_user_id=admin.id,
            admin_email=await _admin_email(db, admin),
            action=action,
            endpoint=_endpoint_label(request),
            auth_source=getattr(request.state, "auth_source", "session"),
        )
        try:
            yield ctx
        finally:
            # Audit insert runs in a fresh session so an FK violation or
            # transient DB issue can't roll back the route's session. By
            # this point the response is already built; the audit write is
            # the only thing that benefits from this transaction.
            await _try_commit(ctx)

    return dep


def _endpoint_label(request: Request) -> str:
    """Build the audit ``endpoint`` field as ``"METHOD /path/{template}"``.

    Uses the matched route's path template (e.g. ``/api/admin/users/{user_id}``)
    so audit queries can group by endpoint without having to strip per-request
    IDs out of the URL. Falls back to the resolved path if the route template
    is unavailable (only happens for routes that bypass FastAPI's matching).

    NOTE: ``request.scope["route"]`` is FastAPI/Starlette internal state.
    Stable across the versions we run against today, but a major upgrade
    could change the shape; the ``getattr(..., None)`` fallback degrades
    to the resolved path in that case rather than crashing.
    """
    method = request.method
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{method} {path}"


async def _admin_email(db: AsyncSession, admin: User) -> str | None:
    """Best-effort email for forensic readability.

    Falls back to ``user.user_id`` (the external/Google identifier) if
    the Subscription row is missing. That happens during the very brief
    window between user creation and Subscription auto-provisioning, and
    we'd rather record *something* than nothing.
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == admin.id))
    ).scalar_one_or_none()
    return sub.email if sub and sub.email else getattr(admin, "user_id", None)


async def _try_commit(ctx: AdminAuditContext) -> bool:
    """Insert the audit row in a fresh async session. Returns True on success.

    Failures (FK violations, transient DB issues) log a warning and roll
    back only the audit session; the route's session is untouched.

    Merges ``auth_source`` into the persisted ``detail`` JSON so a
    forensic query can filter on it without a schema change. Routes
    that set their own ``ctx.detail`` keep theirs; this only adds the
    framework-set field. When a route does not touch ``ctx.detail``,
    the persisted dict is ``{"auth_source": "session" | "api_key"}``
    rather than NULL so the column is consistently structured.

    Binds the audit session directly to the async engine instead of
    reusing the route-bound session. That keeps the audit insert on its
    own transaction, both in production and under the test fixture, so
    route cleanup cannot roll back the audit row.
    """
    detail = dict(ctx.detail) if ctx.detail is not None else {}
    detail.setdefault("auth_source", ctx.auth_source)
    audit_session = AsyncSession(bind=get_async_engine(), expire_on_commit=False)
    try:
        row = AdminAuditLog(
            admin_user_id=ctx.admin_user_id,
            admin_email=ctx.admin_email,
            target_user_id=ctx.target_user_id,
            endpoint=ctx.endpoint,
            action=str(ctx.action),
            resource_type=ctx.resource_type,
            resource_id=ctx.resource_id,
            detail=detail,
        )
        audit_session.add(row)
        await audit_session.commit()
        return True
    except Exception as exc:
        with contextlib.suppress(Exception):
            await audit_session.rollback()
        logger.error(
            "admin_audit.commit_failed",
            extra={
                "admin_user_id": ctx.admin_user_id,
                "action": str(ctx.action),
                "error": str(exc),
            },
        )
        return False
    finally:
        with contextlib.suppress(Exception):
            await audit_session.close()
