"""Admin endpoints: user management, usage summary, channel config."""

import datetime
import json
import logging
import unicodedata

from any_llm.exceptions import MissingApiKeyError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.agent.approval import get_approval_store
from backend.app.agent.context import admin_compact_visible_messages, hygiene_compact_memory
from backend.app.agent.file_store import get_user_store
from backend.app.agent.media_staging import STAGING_MAX_PER_USER
from backend.app.auth.admin_dep import get_current_admin
from backend.app.billing.cost import get_user_cost_totals
from backend.app.billing.plans import PLANS
from backend.app.billing.quota import (
    apply_plan_limits_to_current_quota,
    get_current_quota,
    get_usage_summary,
    reset_quota,
)
from backend.app.channels import is_bluebubbles_configured, reset_channel_clients
from backend.app.config import settings, update_settings
from backend.app.config_store import get_settings_store, strip_unchanged_secrets
from backend.app.database import get_async_db
from backend.app.models import (
    AdminApiKey,
    AllowedEmail,
    ChatSession,
    HeartbeatLog,
    IdempotencyKey,
    LLMPayloadCapture,
    LLMUsageLog,
    Message,
    StagedMedia,
    Subscription,
    User,
    WaitlistEntry,
)
from backend.app.schemas import (
    AdminApiKeyCreate,
    AdminApiKeyItem,
    AdminApiKeyListResponse,
    AdminApiKeyMintResponse,
    AdminChannelConfigResponse,
    AdminChannelConfigUpdate,
    AdminChannelRouteEntry,
    AdminHeartbeatLogItem,
    AdminHeartbeatLogListResponse,
    AdminLLMConfigResponse,
    AdminLLMConfigUpdate,
    AdminLLMModelsResponse,
    AdminLLMProvider,
    AdminLLMProvidersResponse,
    AdminStatsResponse,
    AdminToolConfigEntry,
    AdminUsageSummary,
    AdminUserDetailResponse,
    AdminUserLLMOverrideResponse,
    AdminUserLLMOverrideUpdate,
    AdminUserPermissionEntry,
    AdminUserPermissions,
    AdminUserPlanResponse,
    AdminUserPlanUpdate,
    AdminUserResourcePermissionEntry,
    AdminVersionResponse,
    AllowedEmailCreate,
    AllowedEmailListResponse,
    AllowedEmailResponse,
    CompactUserContextRequest,
    CompactUserContextResponse,
    DeleteResponse,
    HygieneCompactMemoryResponse,
    LLMUsageLogItem,
    LLMUsageLogListResponse,
    StagedMediaItem,
    StagedMediaListResponse,
    StatusResponse,
    TelegramWebhookRequest,
    TelegramWebhookResponse,
    UsageBucket,
    UsageSummary,
    UserActiveResponse,
    UserItem,
    UserListResponse,
    WaitlistEntryResponse,
    WaitlistListResponse,
    WebhookEventItem,
    WebhookEventListResponse,
)
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
from backend.app.services.email_service import send_waitlist_approved
from backend.app.services.llm_payload_capture import purge_user_captures
from backend.app.services.llm_service import get_configured_providers, get_models
from backend.app.services.telegram_webhook import register_webhook, unregister_webhook
from backend.app.services.user_deletion import purge_account
from backend.app.version import get_version_info

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------


_VALID_SORTS = {"recent", "oldest", "last_message", "plan", "email", "consent"}

# Common sentinel for "missing timestamp" sort keys. Keep tz-aware so it
# compares cleanly against the tz-aware values that come back from
# `User.created_at` (and the `last_login_at` column, which we promote to UTC
# in `_aware()` below).
_TS_SENTINEL = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def _aware(ts: datetime.datetime | None) -> datetime.datetime | None:
    """Return *ts* with a UTC tzinfo, leaving aware values untouched."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=datetime.UTC)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query(""),
    sort: str = Query("recent"),
    consent: str = Query(
        "all",
        description=(
            "Filter by data-sharing consent. 'all' returns every user, 'shared' "
            "returns only consenting users, 'none' returns only non-consenting "
            "users. Used by the Users tab consent filter."
        ),
    ),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USER_LIST)),
    db: AsyncSession = Depends(get_async_db),
) -> UserListResponse:
    """List users with pagination, search, sort, and consent filter.

    NOTE: This endpoint is O(N) in the total user count today: it loads every
    Subscription, every User row, every Message-this-month aggregate, and the
    full UserData list from the store before slicing the requested page.
    Acceptable at <few-thousand-user scale; revisit when we cross that.
    """
    if sort not in _VALID_SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort '{sort}'. Expected one of: {sorted(_VALID_SORTS)}",
        )
    if consent not in {"all", "shared", "none"}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid consent filter '{consent}'. Expected: all, shared, none.",
        )

    ctx.detail = {
        "offset": offset,
        "limit": limit,
        "search": search,
        "sort": sort,
        "consent": consent,
    }

    # Get subscription data
    subs = (await db.execute(select(Subscription))).scalars().all()
    sub_map: dict[str, Subscription] = {s.user_id: s for s in subs}

    # Messages-this-month per user, aggregated from Message->Session->user_id.
    now = datetime.datetime.now(datetime.UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    msg_rows = (
        await db.execute(
            select(ChatSession.user_id, sa_func.count(Message.id))
            .join(Message, Message.session_id == ChatSession.id)
            .where(Message.timestamp >= period_start)
            .group_by(ChatSession.user_id)
        )
    ).all()
    messages_by_user: dict[str, int] = {uid: int(cnt) for uid, cnt in msg_rows}

    # Last message timestamp per user, taken from ChatSession.last_message_at
    # (updated by the agent on each inbound/outbound message). This is what
    # the admin table actually wants to surface: when users are texting in,
    # not when they opened the SPA.
    last_msg_rows = (
        await db.execute(
            select(ChatSession.user_id, sa_func.max(ChatSession.last_message_at)).group_by(
                ChatSession.user_id
            )
        )
    ).all()
    last_message_by_user: dict[str, datetime.datetime | None] = {
        uid: _aware(ts) for uid, ts in last_msg_rows
    }

    # Login + signup timestamps from the users table. last_login_at lives on
    # the User __table__ (added via user_extensions) but not as a Python mapped
    # attribute, so use the column directly. We promote naive values to UTC
    # so the sort comparators don't mix naive and aware datetimes.
    last_login_col = User.__table__.c.last_login_at
    login_rows = (await db.execute(select(User.id, User.created_at, last_login_col))).all()
    login_map: dict[str, tuple[datetime.datetime | None, datetime.datetime | None]] = {
        uid: (_aware(created), _aware(last_login)) for uid, created, last_login in login_rows
    }

    # Per-user conversation count and consent snapshot. Read both in a
    # single pass so the table can render "shared (3 days ago) | 12
    # conversations" without making the admin drill into the Shared tab.
    convo_rows = (
        await db.execute(
            select(ChatSession.user_id, sa_func.count(ChatSession.id)).group_by(ChatSession.user_id)
        )
    ).all()
    convo_count_by_user: dict[str, int] = {uid: int(cnt) for uid, cnt in convo_rows}

    consent_rows = (
        await db.execute(
            select(
                User.id,
                User.data_sharing_consent,
                User.data_sharing_consent_at,
            )
        )
    ).all()
    consent_by_user: dict[str, tuple[bool, datetime.datetime | None]] = {
        uid: (bool(flag), _aware(ts)) for uid, flag, ts in consent_rows
    }

    # Get all users from file store
    store = get_user_store()
    all_users = await store.list_all_async()

    # Apply consent filter before search so the search-no-results message
    # is accurate for the active filter ("no consenting users match X").
    if consent == "shared":
        all_users = [u for u in all_users if consent_by_user.get(u.id, (False, None))[0]]
    elif consent == "none":
        all_users = [u for u in all_users if not consent_by_user.get(u.id, (False, None))[0]]

    # Apply search filter (matches user_id or email)
    if search:
        pattern = search.lower()
        all_users = [
            u
            for u in all_users
            if pattern in u.user_id.lower()
            or pattern in (sub_map[u.id].email.lower() if u.id in sub_map else "")
        ]

    def _signup_ts(u: object) -> datetime.datetime:
        created = login_map.get(u.id, (None, None))[0]  # type: ignore[attr-defined]
        return created or _TS_SENTINEL

    def _last_message_ts(u: object) -> datetime.datetime:
        last = last_message_by_user.get(u.id)  # type: ignore[attr-defined]
        return last or _TS_SENTINEL

    def _plan(u: object) -> str:
        sub = sub_map.get(u.id)  # type: ignore[attr-defined]
        return sub.plan if sub else "free"

    def _email(u: object) -> str:
        sub = sub_map.get(u.id)  # type: ignore[attr-defined]
        return sub.email if sub else u.user_id  # type: ignore[attr-defined]

    def _consent_ts(u: object) -> datetime.datetime:
        # Sort: consenting users first, ordered by most recent opt-in.
        flag, ts = consent_by_user.get(u.id, (False, None))  # type: ignore[attr-defined]
        if not flag:
            return _TS_SENTINEL
        return ts or _TS_SENTINEL

    if sort == "recent":
        all_users.sort(key=_signup_ts, reverse=True)
    elif sort == "oldest":
        all_users.sort(key=_signup_ts)
    elif sort == "last_message":
        all_users.sort(key=_last_message_ts, reverse=True)
    elif sort == "plan":
        all_users.sort(key=_plan)
    elif sort == "email":
        all_users.sort(key=lambda u: _email(u).lower())
    elif sort == "consent":
        all_users.sort(key=_consent_ts, reverse=True)

    total = len(all_users)
    page = all_users[offset : offset + limit]

    def _iso(ts: datetime.datetime | None) -> str | None:
        aware = _aware(ts)
        return aware.isoformat() if aware is not None else None

    return UserListResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[
            UserItem(
                id=u.id,
                user_id=u.user_id,
                email=sub_map[u.id].email if u.id in sub_map else "",
                plan=sub_map[u.id].plan if u.id in sub_map else "free",
                status=sub_map[u.id].status if u.id in sub_map else "none",
                role=sub_map[u.id].role if u.id in sub_map else "user",
                is_active=u.is_active,
                onboarding_complete=u.onboarding_complete,
                created_at=_iso(login_map.get(u.id, (None, None))[0]),
                last_login_at=_iso(login_map.get(u.id, (None, None))[1]),
                last_message_at=_iso(last_message_by_user.get(u.id)),
                messages_this_month=messages_by_user.get(u.id, 0),
                data_sharing_consent=consent_by_user.get(u.id, (False, None))[0],
                data_sharing_consent_at=_iso(consent_by_user.get(u.id, (False, None))[1]),
                conversation_count=convo_count_by_user.get(u.id, 0),
            )
            for u in page
        ],
    )


# Cap input length before masking. ``ChannelRoute.channel_identifier`` is
# stored as an unbounded String in OSS, so a malicious or malformed value
# could blow up the masked output (each ``·`` is ~6 bytes JSON-encoded as
# ``\\u00b7``). 128 is well above any real phone / email / handle.
_MAX_IDENTIFIER_LEN = 128


def _strip_bidi_controls(s: str) -> str:
    """Drop Unicode format/Cf codepoints (RTL / LTR overrides etc.).

    Bidi controls let an attacker render a stored identifier in a way the
    admin reading the masked output won't recognize (``a***@b***.‮txe``
    visually presents as ``ext`` due to RTL override). Strip them from
    the source before assembling the mask so the output cannot be
    visually reordered.
    """
    return "".join(c for c in s if unicodedata.category(c) != "Cf")


def _is_phone_shaped(s: str) -> bool:
    """Detect ``+15551234567`` or ``15551234567`` style runs of digits."""
    candidate = s[1:] if s.startswith("+") else s
    return bool(candidate) and candidate.isdigit()


def _mask_channel_identifier(identifier: str) -> str:
    """Mask the user-routing identifier (phone, email, Telegram chat id).

    Channel identifiers are PII (phone numbers, iMessage emails, Telegram
    chat IDs). Admins legitimately need to *recognize* a route — to
    confirm "this is the user from ticket #123" or to verify the right
    last-4 digits with the user — but they don't need the raw value at a
    glance. We show a prefix + suffix that's enough for recognition and
    last-4 confirmation, never enough to dial / message directly off the
    admin panel without going through user search.

    Steps:

    1. Strip Unicode bidi controls so the masked output cannot be
       visually reordered by attacker-stored RTL overrides.
    2. Cap input length so a pathological 1MB stored value can't
       amplify the JSON response.
    3. Email-shape (``local@host.tld``): mask local, host, AND tld so
       enterprise/staging domains don't leak in cleartext. Phone-shaped
       local parts (``+15555550@example.com``) are fully replaced rather
       than echoing the leading ``+`` as a structural hint.
    4. E.164 phone (``+...``): show country/area + last 4.
    5. Generic head+tail mask for everything else, with the head/tail
       size scaling down for shorter identifiers so we never reveal more
       than ~30% of any value.

    Identifiers shorter than the smallest meaningful mask are returned
    verbatim — there's nothing useful to mask in 3-4 chars.
    """
    if not identifier:
        return identifier

    identifier = _strip_bidi_controls(identifier)
    if not identifier:
        return identifier

    if len(identifier) > _MAX_IDENTIFIER_LEN:
        identifier = identifier[:_MAX_IDENTIFIER_LEN]

    # Email-shaped (``foo@example.com``) — fires only when the domain has
    # a ``.``; bare ``@handle`` values fall through to the generic mask.
    if "@" in identifier:
        local, _, domain = identifier.partition("@")
        if "." in domain:
            host, _, tld = domain.rpartition(".")
            # Phone-shaped local: don't echo the leading ``+``; the full
            # token tells the admin "this is a phone-aliased email" and
            # masks every digit.
            if _is_phone_shaped(local):
                masked_local = "***"
            else:
                masked_local = (local[:1] + "***") if local else "***"
            masked_host = (host[:1] + "***") if host else "***"
            # TLD is masked too — custom enterprise / staging TLDs would
            # otherwise leak the company name. Keep the leading char as a
            # weak hint (``.com``-shape vs ``.local``) without exposing
            # the full label.
            masked_tld = (tld[:1] + "**") if tld else "***"
            return f"{masked_local}@{masked_host}.{masked_tld}"
        # else: handle-shaped (``@somebot``) or missing-domain (``x@``);
        # fall through to the generic head/tail mask.

    # Phone number (E.164 ``+15551234567``): show country/area + last 4.
    # Floor at len > 8 so prefix and suffix slices don't abut/overlap;
    # shorter phones (8-char ``+1234567`` etc.) fall through to generic.
    if identifier.startswith("+") and len(identifier) > 8:
        return identifier[:4] + "·" * (len(identifier) - 8) + identifier[-4:]

    # Generic head+tail mask for handles, numeric IDs, short emails. The
    # head/tail size scales down for shorter identifiers — a 5-char value
    # masked at head=2+tail=2 leaves only 1 char hidden (40% leak); we
    # use head=1+tail=2 for short inputs and head=2+tail=2 only when the
    # identifier is at least 10 chars (so the leak ratio drops to ≤30%).
    n = len(identifier)
    if n >= 10:
        head, tail = 2, 2
    elif n >= 5:
        head, tail = 1, 2
    else:
        return identifier  # nothing meaningful to mask in <5 chars
    return identifier[:head] + "·" * (n - head - tail) + identifier[-tail:]


def _build_permissions(data: dict[str, object]) -> AdminUserPermissions:
    """Flatten the OSS approval store's JSON document for admin display.

    Inputs come from ``ApprovalStore.load_user_permissions``, which falls
    back to an empty default shape on missing or malformed rows. We
    defensively skip any entries whose value isn't a string. The OSS
    write paths constrain levels to ``always``/``ask``/``deny``, but a
    legacy or hand-edited row could still be lurking.
    """
    tools_obj = data.get("tools")
    raw_tools = tools_obj if isinstance(tools_obj, dict) else {}
    permission_tools = [
        AdminUserPermissionEntry(tool_name=tn, level=lv)
        for tn, lv in sorted(raw_tools.items())
        if isinstance(tn, str) and isinstance(lv, str)
    ]

    resources_obj = data.get("resources")
    raw_resources = resources_obj if isinstance(resources_obj, dict) else {}
    permission_resources: list[AdminUserResourcePermissionEntry] = []
    for tn in sorted(raw_resources.keys()):
        if not isinstance(tn, str):
            continue
        res_map = raw_resources[tn]
        if not isinstance(res_map, dict):
            continue
        for resource, lv in sorted(res_map.items()):
            if isinstance(resource, str) and isinstance(lv, str):
                permission_resources.append(
                    AdminUserResourcePermissionEntry(tool_name=tn, resource=resource, level=lv)
                )

    return AdminUserPermissions(tools=permission_tools, resources=permission_resources)


@router.get("/users/{user_id}", response_model=AdminUserDetailResponse)
async def get_user_detail(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USER_DETAIL)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUserDetailResponse:
    """Return identity, subscription, profile config, and integrations.

    Slimmed in #325 work item 2: user-authored content (memory, soul,
    user text, heartbeat directives, message bodies, tool-call args /
    results) was removed from this default response. Content surfaces
    only via the consent-gated paths — ``/admin/reported-conversations``
    (after a user reports a conversation) or ``/admin/shared-data``
    (when a user opted into data sharing) — once items 3 + 4 land.

    Eager-loads ``tool_configs`` and ``channel_routes`` via selectinload
    so we make one round-trip instead of three.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id

    user = (
        await db.execute(
            select(User)
            .options(
                selectinload(User.tool_configs),
                selectinload(User.channel_routes),
            )
            .where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    ctx.target_user_id = user_id
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()

    # Sub-tool gating is no longer on ``tool_configs``: OSS #1323
    # collapsed ``tool_configs.disabled_sub_tools`` into the
    # ``user_permissions`` per-tool ``"never"`` level. Surfacing that
    # state in the admin user-detail response would require a join on
    # ``user_permissions`` and a new schema field; defer until an admin
    # asks for it.
    tool_configs_out = [
        AdminToolConfigEntry(tool_name=tc.name, enabled=tc.enabled)
        for tc in sorted(user.tool_configs, key=lambda t: t.name)
    ]

    # Sort channels by last_inbound_at desc, nulls last. Frontend
    # elevates the first row visually; populated routes come first,
    # most recent at the top. Identifier is masked — see
    # ``_mask_channel_identifier`` for why.
    routes_with_inbound = [c for c in user.channel_routes if c.last_inbound_at is not None]
    routes_without_inbound = [c for c in user.channel_routes if c.last_inbound_at is None]
    routes_with_inbound.sort(key=lambda c: c.last_inbound_at, reverse=True)
    channel_routes_out = [
        AdminChannelRouteEntry(
            channel=cr.channel,
            channel_identifier=_mask_channel_identifier(cr.channel_identifier),
            enabled=cr.enabled,
            last_inbound_at=cr.last_inbound_at.isoformat() if cr.last_inbound_at else None,
        )
        for cr in (*routes_with_inbound, *routes_without_inbound)
    ]

    # Read-only view of the user's tool / resource approval levels. We
    # delegate parsing + default-fallback to the OSS approval store so
    # the JSON shape stays a single source of truth; admins see the
    # same data the user sees at ``GET /user/permissions``.
    permissions_data = await get_approval_store().load_user_permissions(user_id)
    permissions_out = _build_permissions(permissions_data)

    return AdminUserDetailResponse(
        id=user.id,
        user_id=user.user_id,
        email=sub.email if sub else "",
        plan=sub.plan if sub else "free",
        status=sub.status if sub else "none",
        role=sub.role if sub else "user",
        is_active=user.is_active,
        onboarding_complete=user.onboarding_complete,
        subscription_created_at=sub.created_at.isoformat() if sub and sub.created_at else None,
        subscription_updated_at=sub.updated_at.isoformat() if sub and sub.updated_at else None,
        timezone=user.timezone,
        preferred_channel=user.preferred_channel,
        heartbeat_opt_in=user.heartbeat_opt_in,
        heartbeat_frequency=user.heartbeat_frequency,
        tool_configs=tool_configs_out,
        channel_routes=channel_routes_out,
        permissions=permissions_out,
    )


@router.post("/users/{user_id}/activate", response_model=UserActiveResponse)
async def activate_user(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.ACTIVATE_USER)),
    admin: User = Depends(get_current_admin),
) -> UserActiveResponse:
    """Re-activate a deactivated user."""
    # ctx.target_user_id is set only after the existence check passes —
    # the column is FK-constrained, so writing an unknown UUID fails the
    # audit insert and leaves the row unrecorded entirely.
    ctx.resource_type = "user"
    ctx.resource_id = user_id
    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot activate themselves")
    ctx.target_user_id = user_id
    await store.update_async(user_id, is_active=True)
    return UserActiveResponse(id=user_id, is_active=True)


@router.post("/users/{user_id}/deactivate", response_model=UserActiveResponse)
async def deactivate_user(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DEACTIVATE_USER)),
    admin: User = Depends(get_current_admin),
) -> UserActiveResponse:
    """Deactivate a user (soft disable, preserves data)."""
    ctx.resource_type = "user"
    ctx.resource_id = user_id
    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot deactivate themselves")
    ctx.target_user_id = user_id
    await store.update_async(user_id, is_active=False)
    return UserActiveResponse(id=user_id, is_active=False)


@router.delete("/users/{user_id}", response_model=StatusResponse)
async def purge_user(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DELETE_USER)),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_async_db),
) -> StatusResponse:
    """Hard delete a user and every trace of their data.

    Physically removes the user row, cascades to OSS data (sessions,
    messages, channels, media, memory, heartbeats), drops multi-user rows
    (subscription, quotas, archived usage), and clears on-disk data.
    The user can re-onboard cleanly with the same identity afterwards.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot purge themselves")

    # Deliberately NOT setting ``ctx.target_user_id``: by the time the
    # audit row is committed (post-response, in a fresh session), the
    # target user is gone and the FK on ``target_user_id`` would fail
    # the insert. ``resource_id`` already carries the purged user's id
    # for forensic reconstruction.

    await purge_account(db, target, admin_id=admin.id)
    return StatusResponse(status="purged")


@router.post("/users/{user_id}/reset-quota", response_model=UsageSummary)
async def reset_user_quota(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.RESET_QUOTA)),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_async_db),
) -> UsageSummary:
    """Reset a user's current month usage counters to zero."""
    ctx.resource_type = "user"
    ctx.resource_id = user_id
    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot reset their own quota")
    ctx.target_user_id = user_id
    await reset_quota(db, user_id)
    await db.commit()
    return UsageSummary(**await get_usage_summary(db, user_id))


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


# ---------------------------------------------------------------------------
# Heartbeat logs
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/heartbeat-logs", response_model=AdminHeartbeatLogListResponse)
async def get_user_heartbeat_logs(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_HEARTBEAT_LOGS)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminHeartbeatLogListResponse:
    """List heartbeat log metadata for a specific user, most recent first.

    Slimmed in #325 work item 2: ``message_text``, ``reasoning``, and
    ``tasks`` were stripped from this response. Heartbeat content
    surfaces only via the consent-gated paths once items 3 + 4 land.
    """
    ctx.resource_type = "heartbeat_logs"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    store = get_user_store()
    user = await store.get_by_id_async(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    total: int = (
        await db.execute(
            select(sa_func.count(HeartbeatLog.id)).where(HeartbeatLog.user_id == user_id)
        )
    ).scalar_one() or 0

    logs = (
        (
            await db.execute(
                select(HeartbeatLog)
                .where(HeartbeatLog.user_id == user_id)
                .order_by(HeartbeatLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return AdminHeartbeatLogListResponse(
        total=total,
        items=[
            AdminHeartbeatLogItem(
                id=log.id,
                user_id=log.user_id,
                action_type=getattr(log, "action_type", None) or "send",
                channel=getattr(log, "channel", None) or "",
                created_at=log.created_at.isoformat() if log.created_at else "",
            )
            for log in logs
        ],
    )


@router.get("/users/{user_id}/llm-usage-logs", response_model=LLMUsageLogListResponse)
async def get_user_llm_usage_logs(
    user_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_USAGE_LOGS)),
    db: AsyncSession = Depends(get_async_db),
) -> LLMUsageLogListResponse:
    """List per-call LLM usage logs for a specific user, most recent first.

    Each row records a single LLM call: provider, model, purpose
    (primary / vision / heartbeat / compaction / etc.), token counts,
    and cost in USD. Purpose lets you spot a runaway compaction loop
    or a heartbeat model burning cache misses.

    The audit dependency writes one row when the route exits, matching
    the policy on the user-detail endpoint.
    """
    ctx.resource_type = "llm_usage_logs"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    total: int = (
        await db.execute(
            select(sa_func.count(LLMUsageLog.id)).where(LLMUsageLog.user_id == user_id)
        )
    ).scalar_one() or 0

    logs = (
        (
            await db.execute(
                select(LLMUsageLog)
                .where(LLMUsageLog.user_id == user_id)
                .order_by(LLMUsageLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return LLMUsageLogListResponse(
        total=total,
        items=[
            LLMUsageLogItem(
                id=log.id,
                timestamp=log.created_at.isoformat() if log.created_at else "",
                provider=log.provider,
                model=log.model,
                purpose=log.purpose,
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                total_tokens=log.total_tokens,
                cost_usd=str(log.cost),
                cache_creation_input_tokens=log.cache_creation_input_tokens,
                cache_read_input_tokens=log.cache_read_input_tokens,
            )
            for log in logs
        ],
    )


# ---------------------------------------------------------------------------
# Photo pipeline diagnostics: staged media + webhook events
# ---------------------------------------------------------------------------
#
# Bypass-the-DB endpoints for "where did this contractor's photo go"
# investigations. ``staged_media`` is the byte cache the agent reads
# from; ``idempotency_keys`` is the BlueBubbles-webhook-dedup ledger.
# Joining the two against ``messages`` distinguishes "webhook arrived,
# Message persisted" from "webhook arrived, no Message" (consumer-side
# drop) from "webhook never arrived" (Mac sleep or transient outage).


@router.get("/users/{user_id}/staged-media", response_model=StagedMediaListResponse)
async def get_user_staged_media(
    user_id: str,
    limit: int = Query(100, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_STAGED_MEDIA)),
    db: AsyncSession = Depends(get_async_db),
) -> StagedMediaListResponse:
    """List staged-media rows for a user, newest first, with cap context.

    Returns ``total`` / ``active`` / ``uploaded`` / ``cap`` counts so a
    glance answers "is this user pinned at the cap, and are all 50
    slots upload receipts?" -- the shape that proves the receipts-pin
    -the-cap symptom without scrolling the row list.
    """
    ctx.resource_type = "staged_media"
    ctx.resource_id = user_id
    ctx.detail = {"limit": limit}

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    now = datetime.datetime.now(datetime.UTC)
    total: int = (
        await db.execute(
            select(sa_func.count(StagedMedia.id)).where(StagedMedia.user_id == user_id)
        )
    ).scalar_one() or 0
    active: int = (
        await db.execute(
            select(sa_func.count(StagedMedia.id)).where(
                StagedMedia.user_id == user_id,
                StagedMedia.expires_at > now,
            )
        )
    ).scalar_one() or 0
    uploaded: int = (
        await db.execute(
            select(sa_func.count(StagedMedia.id)).where(
                StagedMedia.user_id == user_id,
                StagedMedia.upload_status.isnot(None),
            )
        )
    ).scalar_one() or 0

    rows = (
        (
            await db.execute(
                select(StagedMedia)
                .where(StagedMedia.user_id == user_id)
                .order_by(StagedMedia.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return StagedMediaListResponse(
        total=total,
        active=active,
        uploaded=uploaded,
        cap=STAGING_MAX_PER_USER,
        items=[
            StagedMediaItem(
                handle=row.handle,
                original_url=row.original_url,
                mime_type=row.mime_type,
                created_at=row.created_at.isoformat() if row.created_at else "",
                expires_at=row.expires_at.isoformat() if row.expires_at else "",
                upload_service=row.upload_service,
                upload_status=row.upload_status,
                uploaded_at=row.uploaded_at.isoformat() if row.uploaded_at else None,
            )
            for row in rows
        ],
    )


@router.get("/users/{user_id}/webhook-events", response_model=WebhookEventListResponse)
async def get_user_webhook_events(
    user_id: str,
    since: str = Query(
        "",
        description=(
            "ISO timestamp lower bound. Default returns the most recent rows. "
            "Format ``2026-05-21T00:00:00+00:00``."
        ),
    ),
    channel_prefix: str = Query(
        "bb_",
        description=(
            "Restrict to ``idempotency_keys`` whose ``external_id`` starts "
            "with this prefix. Defaults to ``bb_`` (BlueBubbles)."
        ),
    ),
    include_orphans: bool = Query(
        False,
        description=(
            "Also return idempotency rows in the window that did NOT produce "
            "a Message row. Orphans are not user-scoped (the idempotency "
            "table has no user_id column) so they may belong to another "
            "tenant; only opt in for whole-server investigations."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_WEBHOOK_EVENTS)),
    db: AsyncSession = Depends(get_async_db),
) -> WebhookEventListResponse:
    """List webhook dedup events for this user, newest first.

    Joins ``idempotency_keys`` to ``messages`` via the external_id so
    each row carries whether a Message persisted. Default behavior is
    to return only events tied to this user; setting
    ``include_orphans=true`` adds rows where no Message landed (those
    reveal approval-gate consumption or consumer-side failures, but
    cannot be attributed to a specific user so they are off by
    default to avoid cross-tenant noise).
    """
    ctx.resource_type = "webhook_events"
    ctx.resource_id = user_id
    ctx.detail = {
        "limit": limit,
        "channel_prefix": channel_prefix,
        "since": since,
        "include_orphans": include_orphans,
    }

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    since_dt: datetime.datetime | None = None
    if since:
        try:
            since_dt = datetime.datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid since timestamp: {exc}",
            ) from exc

    # LEFT JOIN keeps a row even when no Message landed. Two flavors:
    # - Default: only rows whose joined Message belongs to ``user_id``
    #   (an INNER-join semantic implemented as a WHERE clause that
    #   discards orphans).
    # - ``include_orphans=true``: also keep rows with NULL message join,
    #   which surfaces cross-user orphans for diagnostic use.
    stmt = (
        select(
            IdempotencyKey,
            Message,
            ChatSession.user_id.label("msg_user_id"),
        )
        .join(Message, Message.external_message_id == IdempotencyKey.external_id, isouter=True)
        .join(ChatSession, ChatSession.id == Message.session_id, isouter=True)
        .where(IdempotencyKey.external_id.startswith(channel_prefix))
    )
    if since_dt is not None:
        stmt = stmt.where(IdempotencyKey.created_at >= since_dt)
    if include_orphans:
        stmt = stmt.where((ChatSession.user_id == user_id) | (Message.id.is_(None)))
    else:
        stmt = stmt.where(ChatSession.user_id == user_id)
    stmt = stmt.order_by(IdempotencyKey.created_at.desc()).limit(limit)
    rows = (await db.execute(stmt)).all()

    items: list[WebhookEventItem] = []
    for key_row, msg_row, msg_user_id in rows:
        media_count = 0
        if msg_row is not None and msg_row.media_urls_json:
            try:
                parsed = json.loads(msg_row.media_urls_json)
                media_count = len(parsed) if isinstance(parsed, list) else 0
            except (ValueError, TypeError):
                media_count = 0
        items.append(
            WebhookEventItem(
                external_id=key_row.external_id,
                created_at=key_row.created_at.isoformat() if key_row.created_at else "",
                message_persisted=msg_row is not None,
                user_id=msg_user_id,
                message_timestamp=(
                    msg_row.timestamp.isoformat() if msg_row and msg_row.timestamp else None
                ),
                media_count=media_count,
            )
        )

    return WebhookEventListResponse(total=len(items), items=items)


# ---------------------------------------------------------------------------
# LLM payload export (consent-gated)
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/llm-payloads")
async def export_user_llm_payloads(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.EXPORT_LLM_PAYLOADS)),
    db: AsyncSession = Depends(get_async_db),
) -> JSONResponse:
    """Download the captured LLM request payloads for one user.

    Returns the previous-era and current-era payload snapshots stored
    by ``llm_payload_capture`` for users who have toggled
    ``data_sharing_consent``. Non-consenting users are not captured in
    the first place, so 404 is returned both when no row exists and
    when the user has revoked consent (which deletes the row).

    Response is served with ``Content-Disposition: attachment`` so the
    admin frontend can drop it to disk for offline analysis without
    rendering the JSON in-page.
    """
    ctx.resource_type = "llm_payload_captures"
    ctx.resource_id = user_id

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    # Defense-in-depth: if consent has been revoked since the last
    # capture, purge any lingering row and 404. The capture observer
    # also lazy-cleans on the next LLM call, but an admin reading
    # before that fires would otherwise see stale data.
    #
    # The ``db.commit()`` here is safe alongside the audit dependency:
    # ``audit_admin`` writes its row in a fresh session bound to the
    # engine (see services/admin_audit.py), not in ``db``. If that
    # contract ever changes, the commit-then-raise pattern would skip
    # the audit row for revoked-consent reads -- worth a regression
    # test if the audit dep is refactored to share ``db``.
    if not user.data_sharing_consent:
        await purge_user_captures(db, user_id)
        await db.commit()
        raise HTTPException(status_code=404, detail="No captured payloads for this user")

    row = (
        await db.execute(select(LLMPayloadCapture).where(LLMPayloadCapture.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No captured payloads for this user")

    body = {
        "user_id": user_id,
        "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
        # ``latest_capture_at`` mirrors ``current_era.captured_at`` at the
        # top level so admins can eyeball "did this user just hit a runaway
        # context loop?" without having to dig into the nested object.
        "latest_capture_at": row.current_era_captured_at.isoformat(),
        "current_era": {
            "payload": row.current_era_payload,
            "captured_at": row.current_era_captured_at.isoformat(),
            "min_message_seq": row.current_era_min_message_seq,
            "request_id": row.current_era_request_id,
            "payload_bytes": row.current_era_payload_bytes,
            "response": row.current_era_response,
            "response_captured_at": (
                row.current_era_response_captured_at.isoformat()
                if row.current_era_response_captured_at is not None
                else None
            ),
            "response_bytes": row.current_era_response_bytes,
        },
        "previous_era": (
            {
                "payload": row.previous_era_payload,
                "captured_at": (
                    row.previous_era_captured_at.isoformat()
                    if row.previous_era_captured_at is not None
                    else None
                ),
                "min_message_seq": row.previous_era_min_message_seq,
                "request_id": row.previous_era_request_id,
                "payload_bytes": row.previous_era_payload_bytes,
                "response": row.previous_era_response,
                "response_captured_at": (
                    row.previous_era_response_captured_at.isoformat()
                    if row.previous_era_response_captured_at is not None
                    else None
                ),
                "response_bytes": row.previous_era_response_bytes,
            }
            if row.previous_era_payload is not None
            else None
        ),
    }
    ctx.detail = {
        "current_bytes": row.current_era_payload_bytes,
        "previous_bytes": row.previous_era_payload_bytes,
        "has_previous": row.previous_era_payload is not None,
        "current_response_bytes": row.current_era_response_bytes,
        "previous_response_bytes": row.previous_era_response_bytes,
        "has_current_response": row.current_era_response is not None,
    }
    return JSONResponse(
        content=body,
        headers={"Content-Disposition": (f'attachment; filename="llm-payloads-{user_id}.json"')},
    )


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@router.get("/usage/{user_id}", response_model=AdminUsageSummary)
async def get_user_usage(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USAGE)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUsageSummary:
    """Get quota usage and aggregate LLM spend for a specific user.

    Existence-checks the user up-front. ``get_usage_summary`` calls
    ``get_current_quota`` which inserts a ``UsageQuota`` row for the
    user; without the check, a bogus ``user_id`` reaches that insert
    and 500s on the FK to ``users.id``. 404 is the right answer for an
    admin endpoint queried with an unknown user.

    Cost totals are scoped to the same period as the quota counters
    (``period_cost_usd`` covers the current calendar month, matching
    ``messages.used`` / ``tokens.used``). ``lifetime_cost_usd`` lets
    the admin spot a user whose monthly spend is fine but whose
    all-time spend is an outlier.
    """
    ctx.resource_type = "user"
    ctx.resource_id = user_id

    user_exists = (
        await db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none() is not None
    if not user_exists:
        raise HTTPException(status_code=404, detail="User not found")
    ctx.target_user_id = user_id

    quota = await get_current_quota(db, user_id)
    costs = await get_user_cost_totals(db, user_id, quota.period_start)
    return AdminUsageSummary(
        messages=UsageBucket(used=quota.messages_used, limit=quota.messages_limit),
        tokens=UsageBucket(used=quota.tokens_used, limit=quota.tokens_limit),
        period_start=quota.period_start.isoformat() if quota.period_start else None,
        **costs,
    )


# ---------------------------------------------------------------------------
# Allowed emails (registration gating)
# ---------------------------------------------------------------------------


@router.get("/allowed-emails", response_model=AllowedEmailListResponse)
async def list_allowed_emails(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_ALLOWED_EMAILS)),
    db: AsyncSession = Depends(get_async_db),
) -> AllowedEmailListResponse:
    """List all pre-approved email addresses."""
    rows = (await db.execute(select(AllowedEmail).order_by(AllowedEmail.email))).scalars().all()
    ctx.detail = {"count": len(rows)}
    return AllowedEmailListResponse(
        total=len(rows),
        items=[
            AllowedEmailResponse(
                id=r.id,
                email=r.email,
                note=r.note,
                created_at=r.created_at.isoformat() if r.created_at else "",
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
        created_at=entry.created_at.isoformat() if entry.created_at else "",
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


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


@router.get("/waitlist", response_model=WaitlistListResponse)
async def list_waitlist_entries(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_WAITLIST)),
    db: AsyncSession = Depends(get_async_db),
) -> WaitlistListResponse:
    """List waitlist entries, newest first."""
    ctx.detail = {"offset": offset, "limit": limit}
    total = (await db.execute(select(sa_func.count(WaitlistEntry.id)))).scalar_one() or 0
    rows = (
        (
            await db.execute(
                select(WaitlistEntry)
                .order_by(WaitlistEntry.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
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
                created_at=r.created_at.isoformat() if r.created_at else "",
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
        created_at=allowed.created_at.isoformat() if allowed.created_at else "",
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


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_STATS)),
) -> AdminStatsResponse:
    """Return the messaging configuration needed by the admin overview."""
    return AdminStatsResponse(
        telegram_configured=bool(settings.telegram_bot_token),
        bluebubbles_configured=is_bluebubbles_configured(),
        twilio_configured=bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret
        ),
    )


# ---------------------------------------------------------------------------
# Version metadata (admin overview card + auto-reload poll)
# ---------------------------------------------------------------------------


@router.get("/version", response_model=AdminVersionResponse)
async def get_admin_version(
    _admin: User = Depends(get_current_admin),
) -> AdminVersionResponse:
    """Build metadata for the admin overview card and the client's auto-reload poll."""
    return AdminVersionResponse(**get_version_info())


# ---------------------------------------------------------------------------
# Channel config (server-level settings)
# ---------------------------------------------------------------------------


def _build_admin_channel_config() -> AdminChannelConfigResponse:
    return AdminChannelConfigResponse(
        bluebubbles_server_url=settings.bluebubbles_server_url,
        bluebubbles_password_set=bool(settings.bluebubbles_password),
        bluebubbles_imessage_address=settings.bluebubbles_imessage_address,
        bluebubbles_send_method=settings.bluebubbles_send_method,
        bluebubbles_configured=is_bluebubbles_configured(),
        telegram_bot_token_set=bool(settings.telegram_bot_token),
        telegram_allowed_chat_id=settings.telegram_allowed_chat_id,
        linq_api_token_set=bool(settings.linq_api_token),
        linq_from_number=settings.linq_from_number,
        linq_allowed_numbers=settings.linq_allowed_numbers,
        linq_preferred_service=settings.linq_preferred_service,
        twilio_account_sid_set=bool(settings.twilio_account_sid),
        twilio_auth_token_set=bool(settings.twilio_auth_token),
        twilio_api_key_sid_set=bool(settings.twilio_api_key_sid),
        twilio_api_key_secret_set=bool(settings.twilio_api_key_secret),
        twilio_configured=bool(
            settings.twilio_account_sid
            and settings.twilio_auth_token
            and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret
        ),
        twilio_phone_number=settings.twilio_phone_number,
        twilio_messaging_service_sid=settings.twilio_messaging_service_sid,
        twilio_allowed_numbers=settings.twilio_allowed_numbers,
    )


@router.get("/channels/config", response_model=AdminChannelConfigResponse)
async def get_admin_channel_config(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_CHANNEL_CONFIG)),
) -> AdminChannelConfigResponse:
    """Return full server-level channel configuration for the admin panel."""
    return _build_admin_channel_config()


@router.put("/channels/config", response_model=AdminChannelConfigResponse)
async def update_admin_channel_config(
    body: AdminChannelConfigUpdate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.UPDATE_CHANNEL_CONFIG)),
) -> AdminChannelConfigResponse:
    """Update server-level channel configuration (admin only)."""
    raw_updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not raw_updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Strip ``MASK`` round-trips for unchanged secret fields (the UI
    # re-submits ``********`` for any secret the admin didn't retype).
    updates = strip_unchanged_secrets(raw_updates)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Record only the keys updated; values may include secrets so never log them.
    ctx.resource_type = "channel_config"
    ctx.detail = {"keys": sorted(updates.keys())}

    # Enforce single Telegram chat ID (no comma-separated lists).
    chat_id = updates.get("telegram_allowed_chat_id", "")
    if chat_id and chat_id != "*" and "," in chat_id:
        raise HTTPException(
            status_code=422,
            detail="Only a single Telegram user ID is allowed. Remove commas.",
        )

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await get_settings_store().save(updates, actor_user_id=ctx.admin_user_id)
    reset_channel_clients(updates)

    return _build_admin_channel_config()


# ---------------------------------------------------------------------------
# Telegram webhook management
# ---------------------------------------------------------------------------


@router.post("/telegram/webhook", response_model=TelegramWebhookResponse)
async def register_telegram_webhook_endpoint(
    body: TelegramWebhookRequest,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.SET_TELEGRAM_WEBHOOK)),
) -> TelegramWebhookResponse:
    """Register or update the Telegram webhook URL.

    If webhook_url is empty, constructs it from APP_BASE_URL.
    """
    ctx.resource_type = "telegram_webhook"
    ok, url = await register_webhook(body.webhook_url)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to register Telegram webhook")
    return TelegramWebhookResponse(status="registered", webhook_url=url)


@router.delete("/telegram/webhook", response_model=TelegramWebhookResponse)
async def unregister_telegram_webhook_endpoint(
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.DELETE_TELEGRAM_WEBHOOK)),
) -> TelegramWebhookResponse:
    """Remove the Telegram webhook."""
    ctx.resource_type = "telegram_webhook"
    ok = await unregister_webhook()
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to unregister Telegram webhook")
    return TelegramWebhookResponse(status="unregistered", webhook_url="")


# ---------------------------------------------------------------------------
# LLM config: global default + per-user override
# ---------------------------------------------------------------------------


_LLM_GLOBAL_FIELDS: frozenset[str] = frozenset({"llm_provider", "llm_model", "llm_api_base"})


def _build_llm_config_response() -> AdminLLMConfigResponse:
    return AdminLLMConfigResponse(
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_api_base=settings.llm_api_base,
    )


@router.get("/config/llm", response_model=AdminLLMConfigResponse)
async def get_admin_llm_config(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_CONFIG)),
) -> AdminLLMConfigResponse:
    """Return the global default LLM provider/model used when a user has no override."""
    return _build_llm_config_response()


@router.put("/config/llm", response_model=AdminLLMConfigResponse)
async def update_admin_llm_config(
    body: AdminLLMConfigUpdate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.UPDATE_LLM_CONFIG)),
) -> AdminLLMConfigResponse:
    """Update the global default LLM. Persisted to the settings store."""
    updates: dict[str, str] = {}
    for key in _LLM_GLOBAL_FIELDS:
        value = getattr(body, key, None)
        if value is not None:
            updates[key] = value
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    ctx.resource_type = "llm_config"
    ctx.detail = {"keys": sorted(updates.keys())}

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await get_settings_store().save(updates, actor_user_id=ctx.admin_user_id)
    return _build_llm_config_response()


def _override_response(sub: Subscription) -> AdminUserLLMOverrideResponse:
    """Build a response showing both the override and the effective values.

    Empty override fields mean "fall back to global". The effective
    fields tell the admin exactly what the agent will use for this user.
    """
    return AdminUserLLMOverrideResponse(
        user_id=sub.user_id,
        llm_provider_override=sub.llm_provider_override or "",
        llm_model_override=sub.llm_model_override or "",
        effective_llm_provider=sub.llm_provider_override or settings.llm_provider,
        effective_llm_model=sub.llm_model_override or settings.llm_model,
    )


async def _get_subscription_or_404(db: AsyncSession, user_id: str) -> Subscription:
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="User not found")
    return sub


@router.get(
    "/users/{user_id}/llm-config",
    response_model=AdminUserLLMOverrideResponse,
)
async def get_user_llm_config(
    user_id: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_USER_LLM_OVERRIDE)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUserLLMOverrideResponse:
    """Return per-user LLM override (and the effective values after fallback)."""
    sub = await _get_subscription_or_404(db, user_id)
    ctx.target_user_id = user_id
    ctx.resource_type = "user_llm_override"
    return _override_response(sub)


@router.put(
    "/users/{user_id}/llm-config",
    response_model=AdminUserLLMOverrideResponse,
)
async def update_user_llm_config(
    user_id: str,
    body: AdminUserLLMOverrideUpdate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.UPDATE_USER_LLM_OVERRIDE)),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUserLLMOverrideResponse:
    """Set the per-user LLM override.

    Pass an empty string in either field to clear that part of the
    override and fall back to the global default. Pass null (omit) to
    leave the field unchanged.

    Deliberately has no self-action guard. Every other user-targeted mutation
    endpoint (plan, activate, deactivate, reset-quota, delete, compact-now,
    hygiene-compact-memory) still rejects a self-targeted call. For the plan /
    activate / deactivate / reset-quota / delete set that guard stops an admin
    escalating their own account: raising their own quota, upgrading their own
    plan, locking themselves out. Which model an admin's own agent talks to is a
    personal preference with no privilege attached, and blocking it stops the
    most common legitimate use (an admin trying a model on their own account
    before rolling it out). ``audit_admin`` still resolves ``get_current_admin``,
    so the role check and the audit record are unaffected.
    """
    sub = await _get_subscription_or_404(db, user_id)
    ctx.target_user_id = user_id
    ctx.resource_type = "user_llm_override"

    payload = body.model_dump(exclude_unset=True)
    changed: list[str] = []
    if "llm_provider_override" in payload:
        new_value = payload["llm_provider_override"] or ""
        if new_value != sub.llm_provider_override:
            sub.llm_provider_override = new_value
            changed.append("llm_provider_override")
    if "llm_model_override" in payload:
        new_value = payload["llm_model_override"] or ""
        if new_value != sub.llm_model_override:
            sub.llm_model_override = new_value
            changed.append("llm_model_override")

    if not changed:
        return _override_response(sub)

    ctx.detail = {
        "keys": changed,
        "values": {k: getattr(sub, k) for k in changed},
    }
    await db.commit()
    await db.refresh(sub)
    return _override_response(sub)


@router.put(
    "/users/{user_id}/plan",
    response_model=AdminUserPlanResponse,
)
async def update_user_plan(
    user_id: str,
    body: AdminUserPlanUpdate,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.UPDATE_USER_PLAN)),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_async_db),
) -> AdminUserPlanResponse:
    """Change a user's plan and re-cap their active month's quota row.

    Without the quota-row update, a mid-month flip would not take effect
    until the next calendar reset because ``UsageQuota`` captures limits
    at row creation. ``messages_used`` / ``tokens_used`` carry over so a
    user partway through their old cap does not get a free reset.
    """
    if body.plan not in PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown plan '{body.plan}'. Known plans: {sorted(PLANS)}",
        )

    sub = await _get_subscription_or_404(db, user_id)
    ctx.target_user_id = user_id
    ctx.resource_type = "user_plan"
    ctx.resource_id = user_id

    if sub.user_id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot change their own plan")

    previous_plan = sub.plan
    if previous_plan != body.plan:
        sub.plan = body.plan
        await apply_plan_limits_to_current_quota(db, user_id, body.plan)
        ctx.detail = {"from": previous_plan, "to": body.plan}
        await db.commit()
        await db.refresh(sub)
    else:
        ctx.detail = {"noop": True, "plan": body.plan}

    quota = await get_current_quota(db, user_id)
    return AdminUserPlanResponse(
        user_id=sub.user_id,
        plan=sub.plan,
        messages_limit=quota.messages_limit,
        tokens_limit=quota.tokens_limit,
    )


# ---------------------------------------------------------------------------
# Provider / model enumeration for the admin LLM config UI
#
# These wrap OSS ``get_models`` (itself a thin wrapper over any-llm's
# ``alist_models``) so the admin UI can render real <select> dropdowns
# instead of free-text inputs. The wrapper translates any-llm exceptions
# into a structured response so the frontend can distinguish "provider does
# not support listing" from "API key missing" from "transient error" and
# degrade gracefully (e.g. fall back to a text input with an inline notice).
#
# Admin-only by design: the underlying OSS endpoint at
# ``/api/user/providers/{provider}/models`` is gated only by
# ``get_current_user``, so any authenticated user can spam an external
# provider call. These admin endpoints add the role check.
# ---------------------------------------------------------------------------


@router.get("/config/llm/providers", response_model=AdminLLMProvidersResponse)
async def list_admin_llm_providers(
    _ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_PROVIDERS)),
) -> AdminLLMProvidersResponse:
    """List all known LLM providers known to any-llm."""
    providers = [AdminLLMProvider(name=p.name, local=p.local) for p in get_configured_providers()]
    return AdminLLMProvidersResponse(providers=providers)


@router.get(
    "/config/llm/providers/{provider}/models",
    response_model=AdminLLMModelsResponse,
)
async def list_admin_llm_provider_models(
    provider: str,
    ctx: AdminAuditContext = Depends(audit_admin(AdminAction.VIEW_LLM_PROVIDER_MODELS)),
) -> AdminLLMModelsResponse:
    """Return models for ``provider`` plus structured failure context.

    Enumerates ``settings.llm_api_base``, matching what the agent loop passes to
    ``amessages`` on every call. Previously this called any-llm with no
    ``api_base`` at all, so the listing went straight to the provider's own API
    while the agent talked to a gateway. On a gateway deployment that failed
    outright, because the gateway's virtual key got presented to the real
    provider ("401 invalid x-api-key"), and an admin could never see the models
    they were actually able to call.

    Deliberately takes no caller-supplied ``api_base``. The OSS sibling
    (``/api/user/providers/{provider}/models``) accepts one because its settings
    form passes it, but the admin UI does not, so the parameter would be
    a curl-only surface whose only real effect is to let an admin make the server
    deliver a provider API key from its environment to an arbitrary host. If the
    admin form ever needs to preview a candidate endpoint before saving it, add
    the parameter together with URL validation, not before.

    Never raises 4xx/5xx for "this provider cannot list models" or "the
    provider's API call failed". Those are normal admin states; the UI
    needs to render them, not see a generic 502. We only let through
    framework-level errors (e.g. validation), which FastAPI handles.
    """
    ctx.resource_type = "llm_provider_models"
    ctx.resource_id = provider

    # Audited so a later "why was this listing empty?" is answerable from the
    # trail, not only from the (rotating) application log.
    effective_api_base = settings.llm_api_base
    ctx.detail = {"api_base": effective_api_base or ""}

    try:
        models = await get_models(provider, api_base=effective_api_base)
    except NotImplementedError as exc:
        return AdminLLMModelsResponse(
            provider=provider,
            models=[],
            supports_listing=False,
            error=str(exc) or "This provider does not support listing models.",
        )
    except MissingApiKeyError as exc:
        return AdminLLMModelsResponse(
            provider=provider,
            models=[],
            supports_listing=True,
            error=str(exc),
        )
    except Exception as exc:
        logger.warning(
            "admin.llm.list_models_failed provider=%s api_base=%s error=%s",
            provider,
            effective_api_base or "-",
            exc,
        )
        return AdminLLMModelsResponse(
            provider=provider,
            models=[],
            supports_listing=True,
            error=f"Failed to list models: {exc}",
        )

    return AdminLLMModelsResponse(
        provider=provider,
        models=sorted(models),
        supports_listing=True,
        error=None,
    )


# ---------------------------------------------------------------------------
# Admin API keys (CLI auth)
# ---------------------------------------------------------------------------
#
# Admins use these endpoints to manage long-lived bearer tokens that
# authenticate the same admin endpoints from a CLI / curl context
# where the Google-OAuth-issued JWT can't be driven interactively.
#
# Cleartext tokens are returned exactly once at mint time. The
# storage row holds only the SHA-256 hash; subsequent reads only
# expose the prefix and metadata. The auth path
# (``auth.session_auth.resolve_multi_user``) re-checks the
# owner's admin role on every request, so a key minted today and
# the admin demoted tomorrow stops working immediately, without
# needing to revoke each key one by one.


def _to_api_key_item(row: AdminApiKey) -> AdminApiKeyItem:
    return AdminApiKeyItem(
        id=row.id,
        label=row.label or "",
        key_prefix=row.key_prefix or "",
        created_at=row.created_at.isoformat() if row.created_at else "",
        last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
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
    rows = (
        (
            await db.execute(
                select(AdminApiKey)
                .where(AdminApiKey.user_id == ctx.admin_user_id)
                .order_by(AdminApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
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
        created_at=row.created_at.isoformat() if row.created_at else "",
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
