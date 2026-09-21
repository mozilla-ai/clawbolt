"""Admin console: the user list, the user detail page, and account lifecycle."""

from __future__ import annotations

import datetime
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.agent.approval import get_approval_store
from backend.app.agent.user_db import get_user_store
from backend.app.auth.admin_dep import get_current_admin
from backend.app.billing.quota import (
    get_usage_summary,
    reset_quota,
)
from backend.app.database import get_async_db
from backend.app.models import (
    ChatSession,
    Message,
    Subscription,
    User,
)
from backend.app.query_helpers import iso_or_none
from backend.app.schemas.admin import (
    AdminChannelRouteEntry,
    AdminToolConfigEntry,
    AdminUserDetailResponse,
    AdminUserPermissionEntry,
    AdminUserPermissions,
    AdminUserResourcePermissionEntry,
    UserActiveResponse,
    UserItem,
    UserListResponse,
)
from backend.app.schemas.auth import UsageSummary
from backend.app.schemas.common import StatusResponse
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.user_deletion import purge_account

router = APIRouter()


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
    chat IDs). Admins legitimately need to *recognize* a route, to
    confirm "this is the user from ticket #123" or to verify the right
    last-4 digits with the user, but they don't need the raw value at a
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
    verbatim. There's nothing useful to mask in 3-4 chars.
    """
    if not identifier:
        return identifier

    identifier = _strip_bidi_controls(identifier)
    if not identifier:
        return identifier

    if len(identifier) > _MAX_IDENTIFIER_LEN:
        identifier = identifier[:_MAX_IDENTIFIER_LEN]

    # Email-shaped (``foo@example.com``). Fires only when the domain has
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
            # TLD is masked too. Custom enterprise / staging TLDs would
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
    # head/tail size scales down for shorter identifiers. A 5-char value
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
    only via the consent-gated paths. ``/admin/reported-conversations``
    (after a user reports a conversation) or ``/admin/shared-data``
    (when a user opted into data sharing) once items 3 + 4 land.

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
    # most recent at the top. Identifier is masked; see
    # ``_mask_channel_identifier`` for why.
    routes_with_inbound = [c for c in user.channel_routes if c.last_inbound_at is not None]
    routes_without_inbound = [c for c in user.channel_routes if c.last_inbound_at is None]
    routes_with_inbound.sort(key=lambda c: c.last_inbound_at, reverse=True)
    channel_routes_out = [
        AdminChannelRouteEntry(
            channel=cr.channel,
            channel_identifier=_mask_channel_identifier(cr.channel_identifier),
            enabled=cr.enabled,
            last_inbound_at=iso_or_none(cr.last_inbound_at),
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
    # ctx.target_user_id is set only after the existence check passes.
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
