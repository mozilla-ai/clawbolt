"""Admin console: the global model default, per-user overrides, and model discovery."""

from __future__ import annotations

import logging

from any_llm.exceptions import MissingApiKeyError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth.admin_dep import get_current_admin
from backend.app.billing.plans import PLANS
from backend.app.billing.quota import (
    apply_plan_limits_to_current_quota,
    get_current_quota,
)
from backend.app.config import settings, update_settings
from backend.app.config_store import get_settings_store
from backend.app.database import get_async_db
from backend.app.models import (
    Subscription,
    User,
)
from backend.app.schemas.llm import (
    AdminLLMConfigResponse,
    AdminLLMConfigUpdate,
    AdminLLMModelsResponse,
    AdminLLMProvider,
    AdminLLMProvidersResponse,
    AdminUserLLMOverrideResponse,
    AdminUserLLMOverrideUpdate,
    AdminUserPlanResponse,
    AdminUserPlanUpdate,
)
from backend.app.services.admin_audit import (
    AdminAction,
    AdminAuditContext,
    audit_admin,
)
from backend.app.services.llm_endpoints import missing_endpoints
from backend.app.services.llm_service import get_configured_providers, get_models

logger = logging.getLogger(__name__)

router = APIRouter()


_LLM_GLOBAL_FIELDS: frozenset[str] = frozenset(
    {"llm_endpoint", "llm_provider", "llm_model", "llm_api_base", "reasoning_effort"}
)


def _build_llm_config_response() -> AdminLLMConfigResponse:
    return AdminLLMConfigResponse(
        llm_endpoint=settings.llm_endpoint,
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_api_base=settings.llm_api_base,
        reasoning_effort=settings.reasoning_effort,
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

    missing = await missing_endpoints([updates.get("llm_endpoint", "")])
    if missing:
        raise HTTPException(
            status_code=422, detail=f"LLM endpoint {missing[0]!r} is not configured"
        )

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
    # Endpoint and provider are inherited as a pair (see
    # ``ClawboltAgent._resolve_target``), so the effective values must be
    # computed as a pair too. Reporting them field by field would tell an
    # admin who pinned a bare provider that the global endpoint still
    # applies, which is the opposite of what the agent will do.
    pinned = bool(sub.llm_endpoint_override or sub.llm_provider_override)
    effective_endpoint = sub.llm_endpoint_override if pinned else settings.llm_endpoint
    effective_provider = sub.llm_provider_override if pinned else settings.llm_provider
    return AdminUserLLMOverrideResponse(
        user_id=sub.user_id,
        llm_endpoint_override=sub.llm_endpoint_override or "",
        llm_provider_override=sub.llm_provider_override or "",
        llm_model_override=sub.llm_model_override or "",
        effective_llm_endpoint=effective_endpoint,
        effective_llm_provider=effective_provider,
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
    if "llm_endpoint_override" in payload:
        new_value = payload["llm_endpoint_override"] or ""
        missing = await missing_endpoints([new_value])
        if missing:
            raise HTTPException(
                status_code=422, detail=f"LLM endpoint {missing[0]!r} is not configured"
            )
        if new_value != sub.llm_endpoint_override:
            sub.llm_endpoint_override = new_value
            changed.append("llm_endpoint_override")
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
