"""Endpoints for user profile management."""

import asyncio
import datetime
import logging
import time
from typing import Any, cast

from any_llm import amessages
from any_llm.exceptions import MissingApiKeyError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.markdown_registry import (
    BudgetExceededError,
    assert_column_within_budget,
)
from backend.app.auth.dependencies import get_current_user
from backend.app.channel_state import realign_preferred_channel_async
from backend.app.channels import is_bluebubbles_configured, reset_channel_clients
from backend.app.config import (
    resolve_imessage_backend,
    settings,
    update_settings,
)
from backend.app.config_store import (
    MASK,
    get_settings_store,
    strip_unchanged_secrets,
)
from backend.app.database import get_async_db
from backend.app.models import ChannelRoute, HeartbeatLog, LLMUsageLog, User
from backend.app.query_helpers import get_or_404_async
from backend.app.schemas import (
    AdminLLMModelsResponse,
    ChannelConfigResponse,
    ChannelConfigUpdate,
    ChannelRouteListResponse,
    ChannelRouteResponse,
    ChannelRouteUpdate,
    DataSharingConsentRequest,
    DataSharingConsentResponse,
    DeleteHeartbeatLogsResponse,
    HeartbeatLogItemResponse,
    HeartbeatLogListResponse,
    LLMEndpointItem,
    LLMEndpointListResponse,
    LLMEndpointTestRequest,
    LLMEndpointTestResult,
    LLMEndpointUpsert,
    LLMUsageByPurpose,
    LLMUsageSummary,
    ModelConfigResponse,
    ModelConfigUpdate,
    ProviderInfo,
    TelegramBotInfoResponse,
    UserProfileResponse,
    UserProfileUpdate,
)
from backend.app.services.admin_audit import AdminAction, record_admin_action
from backend.app.services.llm_endpoints import (
    delete_endpoint,
    endpoint_delete_blockers,
    endpoint_item,
    get_endpoint,
    list_endpoints,
    missing_endpoints,
    resolve_target,
    upsert_endpoint,
)
from backend.app.services.llm_service import (
    LLMTarget,
    get_configured_providers,
    get_models,
    is_local_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _profile_response(c: User) -> UserProfileResponse:
    return UserProfileResponse(
        id=c.id,
        user_id=c.user_id,
        phone=c.phone,
        timezone=c.timezone,
        soul_text=c.soul_text,
        user_text=c.user_text,
        heartbeat_text=c.heartbeat_text,
        preferred_channel=c.preferred_channel,
        channel_identifier=c.channel_identifier,
        heartbeat_opt_in=c.heartbeat_opt_in,
        heartbeat_frequency=c.heartbeat_frequency,
        heartbeat_max_daily=c.heartbeat_max_daily,
        onboarding_complete=c.onboarding_complete,
        is_active=c.is_active,
        data_sharing_consent=c.data_sharing_consent,
        data_sharing_consent_at=(
            c.data_sharing_consent_at.isoformat() if c.data_sharing_consent_at else None
        ),
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


@router.get("/user/profile", response_model=UserProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
) -> UserProfileResponse:
    """Return the current user's profile."""
    return _profile_response(current_user)


@router.put("/user/profile", response_model=UserProfileResponse)
async def update_profile(
    body: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> UserProfileResponse:
    """Partial update of the current user's profile.

    Bounded-growth columns (``user_text``, ``soul_text``,
    ``heartbeat_text``) are routed through
    :func:`backend.app.agent.markdown_registry.assert_column_within_budget`
    so the dashboard editor cannot bypass the byte cap that the agent's
    workspace tools and compaction paths already respect. Returns
    ``413 Payload Too Large`` with the registry's actual / allowed
    sizes so a client-side editor can show a useful error.
    """
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Validate bounded-growth columns up front so a partial commit
    # cannot land before we reject the payload.
    for key, value in updates.items():
        if isinstance(value, str):
            try:
                assert_column_within_budget(key, value)
            except BudgetExceededError as exc:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=str(exc),
                ) from exc

    # Re-query user in the current session to avoid detached instance issues
    user = await get_or_404_async(db, User, detail="User not found", id=current_user.id)

    for key, value in updates.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return _profile_response(user)


# ---------------------------------------------------------------------------
# Data sharing consent
# ---------------------------------------------------------------------------
#
# Separate endpoint (not folded into ``PUT /user/profile``) because the
# timestamp must be stamped on every change (opt-in AND opt-out) so
# consent history is reconstructable. Routing through the generic patch
# endpoint would require a special case for this column; cleaner to give
# it its own URL with the invariant in the route signature.


def _data_sharing_consent_now() -> datetime.datetime:
    """Return ``datetime.datetime.now(datetime.UTC)``.

    Pulled out as a helper so tests can monkeypatch the clock when they
    need to verify the timestamp updated to a specific instant. Without
    this seam, asserting "the second PUT bumped the timestamp" can only
    rely on ``>= t1`` ordering, which is non-trivial when both calls
    happen inside one millisecond of test runtime.
    """
    return datetime.datetime.now(datetime.UTC)


@router.get("/user/data-sharing-consent", response_model=DataSharingConsentResponse)
async def get_data_sharing_consent(
    current_user: User = Depends(get_current_user),
) -> DataSharingConsentResponse:
    """Return the current user's data sharing consent state."""
    return DataSharingConsentResponse(
        data_sharing_consent=current_user.data_sharing_consent,
        data_sharing_consent_at=(
            current_user.data_sharing_consent_at.isoformat()
            if current_user.data_sharing_consent_at
            else None
        ),
    )


@router.put("/user/data-sharing-consent", response_model=DataSharingConsentResponse)
async def update_data_sharing_consent(
    body: DataSharingConsentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> DataSharingConsentResponse:
    """Toggle the current user's data sharing consent.

    Stamps ``data_sharing_consent_at`` with the current UTC time on every
    call, regardless of whether the value changed. This makes the column
    a "last toggled at" timestamp rather than a "first opted in at" one,
    which is the cheaper guarantee to keep correct: a one-shot accidental
    double-PUT can't drift the timestamp.
    """
    user = await get_or_404_async(db, User, detail="User not found", id=current_user.id)
    user.data_sharing_consent = body.consent
    user.data_sharing_consent_at = _data_sharing_consent_now()
    await db.commit()
    await db.refresh(user)
    return DataSharingConsentResponse(
        data_sharing_consent=user.data_sharing_consent,
        data_sharing_consent_at=(
            user.data_sharing_consent_at.isoformat() if user.data_sharing_consent_at else None
        ),
    )


# ---------------------------------------------------------------------------
# Channel config
# ---------------------------------------------------------------------------


def _build_channel_config_response() -> ChannelConfigResponse:
    return ChannelConfigResponse(
        telegram_bot_token_set=bool(settings.telegram_bot_token),
        telegram_allowed_chat_id=settings.telegram_allowed_chat_id,
        imessage_backend=resolve_imessage_backend(),
        linq_api_token_set=bool(settings.linq_api_token),
        linq_from_number=settings.linq_from_number,
        linq_allowed_numbers=settings.linq_allowed_numbers,
        linq_preferred_service=settings.linq_preferred_service,
        bluebubbles_configured=is_bluebubbles_configured(),
        bluebubbles_allowed_numbers=settings.bluebubbles_allowed_numbers,
        bluebubbles_imessage_address=settings.bluebubbles_imessage_address,
        twilio_configured=bool(
            settings.twilio_account_sid
            and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret
        ),
        twilio_phone_number=settings.twilio_phone_number,
        twilio_messaging_service_sid=settings.twilio_messaging_service_sid,
        twilio_allowed_numbers=settings.twilio_allowed_numbers,
    )


@router.get("/user/channels/config", response_model=ChannelConfigResponse)
async def get_channel_config(
    _current_user: User = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Return server-level channel configuration."""
    return _build_channel_config_response()


@router.put("/user/channels/config", response_model=ChannelConfigResponse)
async def update_channel_config(
    body: ChannelConfigUpdate,
    current_user: User = Depends(get_current_user),
) -> ChannelConfigResponse:
    """Update server-level channel configuration."""
    raw_updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not raw_updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # The UI re-submits ``MASK`` ("********") for unchanged secret fields;
    # strip those so we don't overwrite real secrets with the sentinel.
    updates = strip_unchanged_secrets(raw_updates)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

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

    await get_settings_store().save(updates, actor_user_id=current_user.id)

    reset_channel_clients(updates)

    return _build_channel_config_response()


@router.get("/user/channels/routes", response_model=ChannelRouteListResponse)
async def get_channel_routes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> ChannelRouteListResponse:
    """Return the current user's channel routes with enabled status."""
    routes = (
        (
            await db.execute(
                select(ChannelRoute)
                .where(ChannelRoute.user_id == current_user.id)
                .order_by(ChannelRoute.created_at)
            )
        )
        .scalars()
        .all()
    )
    return ChannelRouteListResponse(
        routes=[
            ChannelRouteResponse(
                channel=r.channel,
                channel_identifier=r.channel_identifier,
                enabled=r.enabled,
                created_at=r.created_at.isoformat() if r.created_at else "",
                last_inbound_at=r.last_inbound_at.isoformat() if r.last_inbound_at else None,
            )
            for r in routes
        ]
    )


@router.patch("/user/channels/routes/{channel}", response_model=ChannelRouteResponse)
async def update_channel_route(
    channel: str,
    body: ChannelRouteUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> ChannelRouteResponse:
    """Toggle enabled status for a channel route.

    Single-channel enforcement: when enabling a channel, all other
    non-webchat routes for this user are automatically disabled so
    exactly one messaging channel is active at a time.

    If the user has no route and no known identifier for this channel yet
    (fresh onboarding), the selection is persisted via ``preferred_channel``
    only. The route row is created later when the identifier arrives,
    either via an inbound message (OSS) or via an explicit link call
    (premium). This keeps placeholder rows out of the database and avoids
    leaking the user's internal UUID into identifier-shaped UI fields.
    """
    from backend.app.channels import get_channel

    if channel == "webchat":
        raise HTTPException(status_code=400, detail="Webchat is always enabled")
    try:
        get_channel(channel)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}") from exc

    # Re-query user in the current session to avoid detached instance issues
    user = await get_or_404_async(db, User, detail="User not found", id=current_user.id)

    if body.enabled:
        # Single-channel enforcement: disable all other non-webchat routes
        await db.execute(
            update(ChannelRoute)
            .where(
                ChannelRoute.user_id == user.id,
                ChannelRoute.channel != channel,
                ChannelRoute.channel != "webchat",
            )
            .values(enabled=False)
        )

    route = (
        await db.execute(select(ChannelRoute).filter_by(user_id=user.id, channel=channel))
    ).scalar_one_or_none()
    if route is None and user.channel_identifier:
        route = ChannelRoute(
            user_id=user.id,
            channel=channel,
            channel_identifier=user.channel_identifier,
            enabled=body.enabled,
        )
        db.add(route)
    elif route is not None:
        route.enabled = body.enabled

    if body.enabled:
        user.preferred_channel = channel
    else:
        await realign_preferred_channel_async(db, user)

    await db.commit()
    if route is not None:
        await db.refresh(route)
        return ChannelRouteResponse(
            channel=route.channel,
            channel_identifier=route.channel_identifier,
            enabled=route.enabled,
            created_at=route.created_at.isoformat() if route.created_at else "",
            last_inbound_at=route.last_inbound_at.isoformat() if route.last_inbound_at else None,
        )
    return ChannelRouteResponse(
        channel=channel,
        channel_identifier="",
        enabled=body.enabled,
        created_at="",
        last_inbound_at=None,
    )


@router.get("/channels/telegram/bot-info", response_model=TelegramBotInfoResponse)
async def get_telegram_bot_info(
    _current_user: User = Depends(get_current_user),
) -> TelegramBotInfoResponse:
    """Return the Telegram bot username, auto-discovered via getMe."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=404, detail="No Telegram bot token configured")

    try:
        from backend.app.channels import get_channel
        from backend.app.channels.telegram import TelegramChannel

        channel = get_channel("telegram")
        if not isinstance(channel, TelegramChannel):
            raise HTTPException(status_code=404, detail="Telegram channel not available")

        me = await channel.bot.get_me()
        username = me.username or ""
        if not username:
            raise HTTPException(status_code=404, detail="Bot username not available")

        return TelegramBotInfoResponse(
            bot_username=username,
            bot_link=f"https://t.me/{username}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch bot info: {exc}") from exc


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------


def _build_model_config_response() -> ModelConfigResponse:
    return ModelConfigResponse(
        llm_endpoint=settings.llm_endpoint,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_api_base=settings.llm_api_base,
        vision_model=settings.vision_model,
        vision_endpoint=settings.vision_endpoint,
        vision_provider=settings.vision_provider,
        heartbeat_model=settings.heartbeat_model,
        heartbeat_endpoint=settings.heartbeat_endpoint,
        heartbeat_provider=settings.heartbeat_provider,
        compaction_model=settings.compaction_model,
        compaction_endpoint=settings.compaction_endpoint,
        compaction_provider=settings.compaction_provider,
        reasoning_effort=settings.reasoning_effort,
    )


@router.get("/user/model/config", response_model=ModelConfigResponse)
async def get_model_config(
    _current_user: User = Depends(get_current_user),
) -> ModelConfigResponse:
    """Return server-level LLM model configuration."""
    return _build_model_config_response()


@router.put("/user/model/config", response_model=ModelConfigResponse)
async def update_model_config(
    body: ModelConfigUpdate,
    current_user: User = Depends(get_current_user),
) -> ModelConfigResponse:
    """Update server-level LLM model configuration.

    NOTE: In single-tenant (OSS) mode, all authenticated users are
    effectively admins and can modify these settings. The premium layer
    adds role-based guards via AdminConfigGuardMiddleware to restrict
    this to admin users only.
    """
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    missing = await missing_endpoints(
        [
            str(updates.get(key, ""))
            for key in (
                "llm_endpoint",
                "vision_endpoint",
                "heartbeat_endpoint",
                "compaction_endpoint",
            )
        ]
    )
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"LLM endpoint(s) not configured: {', '.join(missing)}",
        )

    try:
        update_settings(updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await get_settings_store().save(updates, actor_user_id=current_user.id)
    return _build_model_config_response()


# ---------------------------------------------------------------------------
# LLM endpoints
# ---------------------------------------------------------------------------


@router.get("/user/model/endpoints", response_model=LLMEndpointListResponse)
async def list_llm_endpoints(
    _current_user: User = Depends(get_current_user),
) -> LLMEndpointListResponse:
    """Return every configured LLM endpoint.

    The single CRUD surface for endpoints, in both tenancy modes. In
    multi-user mode ``middleware.admin_config_guard`` restricts it to admins,
    the same gate the rest of the model config sits behind; in single-user
    mode the one user is the operator.
    """
    return LLMEndpointListResponse(items=[endpoint_item(r) for r in await list_endpoints()])


@router.put("/user/model/endpoints/{name}", response_model=LLMEndpointItem)
async def upsert_llm_endpoint(
    name: str,
    body: LLMEndpointUpsert,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> LLMEndpointItem:
    """Create or replace one endpoint.

    Audited, because an endpoint names both a destination for the
    deployment's traffic and a credential to send with it. "Who pointed us
    at that host" has to be answerable.
    """
    if body.name != name:
        raise HTTPException(status_code=400, detail="Endpoint name in path and body must match")
    if body.dialect not in {p.name for p in get_configured_providers()}:
        raise HTTPException(status_code=422, detail=f"Unknown dialect {body.dialect!r}")

    row = await upsert_endpoint(
        db,
        name=name,
        dialect=body.dialect,
        base_url=body.base_url,
        api_key=body.api_key,
        cache_control=body.cache_control,
        reasoning=body.reasoning,
        pricing=body.pricing,
        notes=body.notes,
    )
    await record_admin_action(
        action=AdminAction.UPSERT_LLM_ENDPOINT,
        admin_user_id=current_user.id,
        endpoint="PUT /api/user/model/endpoints/{name}",
        resource_type="llm_endpoint",
        resource_id=name,
        # Never the key itself, and never a hint at its value: the audit log
        # is read by more people than can set one.
        detail={
            "dialect": body.dialect,
            "base_url": body.base_url,
            "cache_control": body.cache_control,
            "reasoning": body.reasoning,
            "pricing": body.pricing,
            "api_key_changed": body.api_key is not None and body.api_key != MASK,
        },
    )
    return endpoint_item(row)


@router.get(
    "/user/model/endpoints/{name}/models",
    response_model=AdminLLMModelsResponse,
)
async def list_llm_endpoint_models(
    name: str,
    _current_user: User = Depends(get_current_user),
) -> AdminLLMModelsResponse:
    """Enumerate the models a configured endpoint serves.

    The provider-scoped listing route deliberately refuses a caller-supplied
    ``api_base``, because honoring one would make the server deliver a
    provider key from its environment to whatever host the caller named. That
    objection does not reach here: the caller names an endpoint, not a URL,
    and the base and credential come from the stored row. Nothing about the
    destination is caller-controlled.

    Without this the model field falls back to free text whenever an endpoint
    is selected, which is the one case where the operator is least likely to
    know the exact model id by heart.

    Never raises for "this endpoint cannot list models" or "the call failed".
    Both are ordinary states the form has to render, and a 502 would leave it
    with nothing to say.
    """
    row = await get_endpoint(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Endpoint {name!r} not found")

    try:
        models = await asyncio.wait_for(
            get_models(
                row.dialect,
                api_key=row.api_key or None,
                api_base=row.base_url or None,
            ),
            timeout=_LIST_TIMEOUT_SECONDS,
        )
    except NotImplementedError as exc:
        return AdminLLMModelsResponse(
            provider=row.dialect,
            models=[],
            supports_listing=False,
            error=str(exc) or "This endpoint's dialect does not support listing models.",
        )
    except MissingApiKeyError as exc:
        return AdminLLMModelsResponse(
            provider=row.dialect, models=[], supports_listing=True, error=str(exc)
        )
    except Exception as exc:
        logger.warning("Listing models for endpoint %r failed: %s", name, exc)
        return AdminLLMModelsResponse(
            provider=row.dialect,
            models=[],
            supports_listing=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    return AdminLLMModelsResponse(provider=row.dialect, models=models, supports_listing=True)


# A tool schema small enough to be free and real enough to be refused. The
# failure this probe exists to catch only appears when tools and reasoning are
# on the same request: at least one gateway-served model accepts either alone
# and rejects the pair on ``/v1/chat/completions``, so a reachability check
# that sent no tools would pass while every agent turn 400s.
_PROBE_TOOL = {
    "name": "noop",
    "description": "Does nothing. Present only so the request carries a tool.",
    "input_schema": {"type": "object", "properties": {}},
}

# The smallest thinking budget the Anthropic API accepts.
_PROBE_THINKING_BUDGET = 1024

# A gateway that accepts the connection and never answers would otherwise hold
# the request for the provider SDK's own default, which is ten minutes, with
# the Test button disabled the whole time and no way to cancel it.
_PROBE_TIMEOUT_SECONDS = 60.0
_LIST_TIMEOUT_SECONDS = 30.0


def _probe_call_shape(target: LLMTarget) -> tuple[dict[str, Any], int, str]:
    """Reasoning kwargs, ``max_tokens``, and a label, for one probe request.

    A thinking budget must be *smaller* than ``max_tokens``; the Anthropic SDK
    states the rule on ``ThinkingConfigEnabledParam``. Pairing a fixed tiny
    ``max_tokens`` with a configured budget therefore manufactures a 400 on a
    perfectly healthy endpoint, and the probe reports the gateway broken for a
    rule the probe itself broke. Every effort level trips it, since the
    smallest budget is 1024.

    The budget is clamped to that minimum rather than tracking
    ``reasoning_effort``. What this probe answers is whether the endpoint
    accepts reasoning *and tools on the same request*, which is a question
    about the parameter's shape rather than its size, and a probe that spent a
    32768-token budget per click would be too expensive to click freely.
    """
    reasoning = target.reasoning_kwargs(settings.reasoning_effort)
    thinking = reasoning.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = _PROBE_THINKING_BUDGET
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            budget + 64,
            f"thinking, clamped to a {budget}-token budget",
        )
    if "reasoning_effort" in reasoning:
        return reasoning, 256, f"effort={reasoning['reasoning_effort']}"
    if thinking:
        return reasoning, 64, "thinking disabled"
    return {}, 64, "none"


@router.post(
    "/user/model/endpoints/{name}/test",
    response_model=LLMEndpointTestResult,
)
async def test_llm_endpoint(
    name: str,
    body: LLMEndpointTestRequest,
    current_user: User = Depends(get_current_user),
) -> LLMEndpointTestResult:
    """Send one minimal request through an endpoint and report what came back.

    Shaped like a real agent turn rather than a ping: same reasoning
    parameter the endpoint's ``reasoning`` column produces, and a tool
    attached. Both halves matter, and only together. An endpoint can answer a
    bare completion perfectly and still refuse every request the agent
    actually makes.

    ``model`` is optional. Left empty, the endpoint is asked what it serves
    and the first answer is used, which is the common case right after
    creating one. Failures come back as ``ok: false`` with the provider's own
    text, because "your gateway rejected this" is the answer the operator
    needs to read, not a 502.
    """
    row = await get_endpoint(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Endpoint {name!r} not found")

    model = body.model
    if not model:
        try:
            listed = await asyncio.wait_for(
                get_models(row.dialect, api_key=row.api_key or None, api_base=row.base_url or None),
                timeout=_LIST_TIMEOUT_SECONDS,
            )
        except Exception:
            listed = []
        if not listed:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Endpoint {name!r} could not be asked what it serves, so the probe "
                    "needs a model id."
                ),
            )
        model = listed[0]

    target = await resolve_target(endpoint=name, provider="", model=model)
    reasoning, max_tokens, label = _probe_call_shape(target)
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            amessages(
                **target.connection_kwargs(),
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                tools=[dict(_PROBE_TOOL)],
                max_tokens=max_tokens,
                **reasoning,
            ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        ok, detail = True, ""
    except TimeoutError:
        ok, detail = False, f"No answer within {_PROBE_TIMEOUT_SECONDS:.0f}s."
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"

    latency_ms = (time.monotonic() - started) * 1000
    # Spends the operator's credential on an outbound request, so it leaves a
    # record like the writes do. The provider's error text stays out of the
    # trail: it goes to the caller, and the log is read by more people than
    # can set a credential.
    await record_admin_action(
        action=AdminAction.TEST_LLM_ENDPOINT,
        admin_user_id=current_user.id,
        endpoint="POST /api/user/model/endpoints/{name}/test",
        resource_type="llm_endpoint",
        resource_id=name,
        detail={"model": model, "ok": ok, "reasoning": label},
    )
    return LLMEndpointTestResult(
        ok=ok, model=model, detail=detail, latency_ms=latency_ms, reasoning=label
    )


@router.delete("/user/model/endpoints/{name}", status_code=204)
async def delete_llm_endpoint(
    name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """Remove an endpoint, unless something still selects it."""
    blockers = await endpoint_delete_blockers(db, name)
    if blockers:
        raise HTTPException(status_code=409, detail=f"Endpoint {name!r} is {'; '.join(blockers)}")
    if not await delete_endpoint(db, name):
        raise HTTPException(status_code=404, detail=f"Endpoint {name!r} not found")
    await record_admin_action(
        action=AdminAction.DELETE_LLM_ENDPOINT,
        admin_user_id=current_user.id,
        endpoint="DELETE /api/user/model/endpoints/{name}",
        resource_type="llm_endpoint",
        resource_id=name,
    )


# ---------------------------------------------------------------------------
# Provider / model enumeration
# ---------------------------------------------------------------------------


@router.get("/user/providers", response_model=list[ProviderInfo])
async def list_providers(
    _current_user: User = Depends(get_current_user),
) -> list[ProviderInfo]:
    """List available LLM providers from any-llm."""
    return get_configured_providers()


@router.get("/user/providers/{provider}/models")
async def list_provider_models(
    provider: str,
    api_base: str | None = Query(
        None,
        description=(
            "Endpoint to enumerate. Local providers only; a hosted provider always"
            " uses the configured LLM_API_BASE."
        ),
    ),
    _current_user: User = Depends(get_current_user),
) -> list[str]:
    """List available models for a provider.

    ``api_base`` is honored only for a local provider. any-llm resolves a missing
    ``api_key`` from the server's environment, so honoring a caller-supplied base
    for a hosted provider would send that provider's key to whatever host the
    caller named. Local providers are keyless, so there is nothing to send, and
    they are the only case the settings UI uses the field for: the API-base input
    and its "Fetch Models" button render only when the selected provider is
    local, and a hosted provider is always listed with no base.

    This endpoint is gated by ``get_current_user`` alone, which is every user in
    a single-tenant deployment but every *tenant* under a multi-tenant auth
    plugin, so the parameter must not be able to redirect a credentialed call.
    """
    if api_base and not is_local_provider(provider):
        raise HTTPException(
            status_code=400,
            detail=(
                f"api_base is only accepted for a local provider; '{provider}' is"
                " hosted and is listed against the configured LLM_API_BASE."
            ),
        )
    try:
        return await get_models(provider, api_base=api_base)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to list models: {exc}") from exc


# ---------------------------------------------------------------------------
# Heartbeat logs
# ---------------------------------------------------------------------------


@router.get("/user/heartbeat-logs", response_model=HeartbeatLogListResponse)
async def get_heartbeat_logs(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> HeartbeatLogListResponse:
    """List heartbeat logs for the current user, most recent first."""
    total: int = (
        await db.scalar(
            select(sa_func.count(HeartbeatLog.id)).where(HeartbeatLog.user_id == current_user.id)
        )
    ) or 0

    logs = (
        (
            await db.execute(
                select(HeartbeatLog)
                .where(HeartbeatLog.user_id == current_user.id)
                .order_by(HeartbeatLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return HeartbeatLogListResponse(
        total=total,
        items=[
            HeartbeatLogItemResponse(
                id=log.id,
                user_id=log.user_id,
                action_type=log.action_type or "send",
                message_text=log.message_text or "",
                channel=log.channel or "",
                reasoning=log.reasoning or "",
                tasks=log.tasks or "",
                created_at=log.created_at.isoformat() if log.created_at else "",
            )
            for log in logs
        ],
    )


@router.delete("/user/heartbeat-logs", response_model=DeleteHeartbeatLogsResponse)
async def delete_heartbeat_logs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> DeleteHeartbeatLogsResponse:
    """Delete all heartbeat logs for the current user."""
    deleted: int = cast(
        "CursorResult[object]",
        await db.execute(
            delete(HeartbeatLog)
            .where(HeartbeatLog.user_id == current_user.id)
            .execution_options(synchronize_session="fetch")
        ),
    ).rowcount
    await db.commit()
    return DeleteHeartbeatLogsResponse(status="deleted", deleted=deleted)


# ---------------------------------------------------------------------------
# LLM usage summary
# ---------------------------------------------------------------------------


@router.get("/user/llm-usage", response_model=LLMUsageSummary)
async def get_llm_usage(
    days: int = Query(30, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> LLMUsageSummary:
    """Aggregate LLM usage for the current user over the last N days.

    ``total_cost`` sums a column that is zero whenever the cost could not be
    computed, so it is a lower bound. ``unpriced_calls`` is what says so.
    """
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)

    rows = (
        await db.execute(
            select(
                LLMUsageLog.purpose,
                sa_func.count(LLMUsageLog.id).label("call_count"),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.input_tokens), 0).label(
                    "total_input_tokens"
                ),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.output_tokens), 0).label(
                    "total_output_tokens"
                ),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.total_tokens), 0).label("total_tokens"),
                sa_func.coalesce(sa_func.sum(LLMUsageLog.cost), 0).label("total_cost"),
            )
            .where(
                LLMUsageLog.user_id == current_user.id,
                LLMUsageLog.created_at >= since,
            )
            .group_by(LLMUsageLog.purpose)
        )
    ).all()

    by_purpose = [
        LLMUsageByPurpose(
            purpose=row.purpose or "",
            call_count=int(row.call_count),
            total_input_tokens=int(row.total_input_tokens),
            total_output_tokens=int(row.total_output_tokens),
            total_tokens=int(row.total_tokens),
            total_cost=float(row.total_cost),
        )
        for row in rows
    ]

    unpriced = (
        await db.execute(
            select(sa_func.count(LLMUsageLog.id)).where(
                LLMUsageLog.user_id == current_user.id,
                LLMUsageLog.created_at >= since,
                LLMUsageLog.pricing_available.is_(False),
            )
        )
    ).scalar_one()

    return LLMUsageSummary(
        total_calls=sum(p.call_count for p in by_purpose),
        total_tokens=sum(p.total_tokens for p in by_purpose),
        total_cost=sum(p.total_cost for p in by_purpose),
        by_purpose=by_purpose,
        unpriced_calls=int(unpriced),
    )
