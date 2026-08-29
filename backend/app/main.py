import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from any_llm import amessages
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from backend.app.agent.approval import cleanup_orphaned_approvals
from backend.app.agent.compaction_recovery import recover_pending_compactions
from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.agent.inbound_recovery import recover_orphan_inbound_messages
from backend.app.agent.router import set_pipeline_override
from backend.app.auth.dependencies import validate_auth_mode
from backend.app.billing.pipeline_steps import get_multi_user_pipeline
from backend.app.bus import message_bus
from backend.app.channels import get_manager, register_channel
from backend.app.channels.base import (
    BaseChannel,
    channel_route_allowlist,
    set_is_allowed_override,
)
from backend.app.channels.bluebubbles import BlueBubblesChannel
from backend.app.channels.linq import LinqChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.channels.twilio import TwilioChannel
from backend.app.channels.webchat import WebChatChannel
from backend.app.config import (
    log_config_warnings,
    settings,
    validate_imessage_backend,
)
from backend.app.config_store import (
    apply_to_settings,
    get_settings_store,
    import_legacy_config_json,
)
from backend.app.database import db_session_async, get_async_engine
from backend.app.logging_utils import mask_pii
from backend.app.middleware.admin_config_guard import AdminConfigGuardMiddleware
from backend.app.middleware.request_logging import RequestLoggingMiddleware
from backend.app.middleware.security_headers import SecurityHeadersMiddleware
from backend.app.middleware.seo_meta import SeoMetaMiddleware
from backend.app.models import ChannelRoute, User
from backend.app.observability import setup_logging
from backend.app.routers import (
    account,
    admin,
    admin_reported_conversations,
    admin_shared_data,
    app_config,
    auth,
    auth_tokens,
    google_oauth,
    health,
    integrations,
    media_temp,
    monitoring,
    oauth,
    user_calendar,
    user_memory,
    user_permissions,
    user_profile,
    user_sessions,
    user_tools,
    waitlist,
)
from backend.app.routers import (
    channels as channels_router,
)
from backend.app.services.admin_alerts import (
    install_alert_handler,
    start_alert_flusher,
    stop_alert_flusher,
)
from backend.app.services.health_monitor import LOCAL_BASE_URL, health_monitor
from backend.app.services.heartbeat_usage import install_heartbeat_usage_hook
from backend.app.services.llm_payload_capture import install_llm_payload_capture
from backend.app.services.llm_resolver import install_user_llm_resolver
from backend.app.services.oauth import oauth_refresh_scheduler
from backend.app.services.telegram_webhook import discover_bot_username
from backend.app.services.tool_failure_alerts import install_tool_failure_alerts

# Whether this process runs the hosted, multi-tenant surface: OAuth
# sign-in, the admin console, quota enforcement, and operator monitoring.
# Read at import for the agent hooks below, which are process-global and
# cannot be installed per-app. ``create_app`` and the lifespan read
# ``settings.auth_mode`` when they run instead, so a test that builds a
# second app under a different mode gets a consistent one.
MULTI_USER = settings.auth_mode == "multi_user"

# Structured logging with a per-request correlation ID. Pins the root
# logger to WARNING (so httpx does not log request URLs carrying
# credentials at INFO) and raises only the ``backend`` tree to LOG_LEVEL.
setup_logging()

if MULTI_USER:
    # Capture ERROR-level logs for the operator alert email from here on.
    # Installed at import rather than inside setup_logging() because
    # admin_alerts imports observability for the request-id ContextVar, so
    # the reverse import would be a cycle. The flush task that actually
    # sends starts in the lifespan; until then errors only accumulate in
    # memory. Re-installed there as well, because uvicorn's dictConfig
    # strips handlers from the loggers it names (including uvicorn.error)
    # after this module is imported.
    install_alert_handler()

logger = logging.getLogger(__name__)


# -- Build and register channels at module scope ----------------------------

register_channel(TelegramChannel(bot_token=settings.telegram_bot_token))
register_channel(WebChatChannel())
register_channel(LinqChannel())
register_channel(BlueBubblesChannel())
register_channel(TwilioChannel())


# -- Multi-user hooks into the agent stack ----------------------------------

if MULTI_USER:
    # Quota checks before the LLM call and usage tracking after it.
    set_pipeline_override(get_multi_user_pipeline())

    # Senders are approved by having a ChannelRoute, not by an env-var
    # allowlist: an operator-managed list cannot express per-tenant access.
    set_is_allowed_override(channel_route_allowlist)

    # Heartbeat LLM calls bypass the ingestion pipeline, so their spend
    # would otherwise never reach the tenant's UsageQuota counters.
    install_heartbeat_usage_hook()

    # Lets admins pin individual users to a provider/model from the console.
    install_user_llm_resolver()

    # Capture LLM request payloads for users who opted into data sharing,
    # so admins can export them for offline token-efficiency analysis.
    install_llm_payload_capture()

    # Route failed agent tool calls into the operator alert email. Covers the
    # SERVICE and AUTH failures that log at WARNING and so never reach the
    # ERROR-log alerting path at all.
    install_tool_failure_alerts()


async def _enforce_single_channel() -> None:
    """One-time: disable non-preferred routes for existing multi-channel users.

    After the single-channel refactor, each user should have at most one
    enabled messaging channel.  This cleans up users who had multiple
    channels enabled before the refactor.

    Also realigns ``preferred_channel`` if it points to a channel with no
    enabled route while another enabled route exists. This keeps downstream
    consumers (heartbeat, reauth notifications) consistent without needing
    read-time drift-sync.
    """
    async with db_session_async() as db:
        users = (await db.execute(select(User))).scalars().all()
        fixed = 0
        for user in users:
            routes = (
                (await db.execute(select(ChannelRoute).filter_by(user_id=user.id))).scalars().all()
            )
            enabled_messaging = [r for r in routes if r.enabled and r.channel != "webchat"]
            if len(enabled_messaging) > 1:
                preferred_match = next(
                    (r for r in enabled_messaging if r.channel == user.preferred_channel),
                    None,
                )
                # If preferred_channel does not match any enabled route, pick
                # the first enabled messaging route and make it preferred so
                # we never end up with a user whose preferred points to a
                # disabled channel while another is active.
                keeper = preferred_match or enabled_messaging[0]
                for r in enabled_messaging:
                    if r is not keeper:
                        r.enabled = False
                if preferred_match is None:
                    user.preferred_channel = keeper.channel
                fixed += 1
        if fixed:
            await db.commit()
            logger.info(
                "Single-channel enforcement: fixed %d user(s) with multiple enabled channels",
                fixed,
            )


async def _verify_llm_settings() -> None:
    """Verify LLM provider/model settings by making a minimal completion call.

    Surfaces misconfigurations (bad provider, invalid model, missing API key)
    at startup rather than at first user request.  The primary model is
    required; failures for optional model overrides are logged as warnings.
    """
    configs: list[tuple[str, str, str]] = [
        ("primary", settings.llm_provider, settings.llm_model),
    ]
    if settings.vision_model:
        configs.append(
            (
                "vision",
                settings.vision_provider or settings.llm_provider,
                settings.vision_model,
            )
        )
    if settings.compaction_model or settings.compaction_provider:
        configs.append(
            (
                "compaction",
                settings.compaction_provider or settings.llm_provider,
                settings.compaction_model or settings.llm_model,
            )
        )
    if settings.heartbeat_model or settings.heartbeat_provider:
        configs.append(
            (
                "heartbeat",
                settings.heartbeat_provider or settings.llm_provider,
                settings.heartbeat_model or settings.llm_model,
            )
        )

    # Deduplicate by (provider, model) to avoid redundant API calls.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for label, provider, model in configs:
        key = (provider, model)
        if key not in seen:
            seen.add(key)
            unique.append((label, provider, model))

    for label, provider, model in unique:
        try:
            await amessages(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=10,
            )
            logger.info("LLM verified (%s): provider=%s, model=%s", label, provider, model)
        except Exception as exc:
            if label == "primary":
                raise RuntimeError(
                    f"LLM startup check failed for {label} model "
                    f"(LLM_PROVIDER={provider!r}, LLM_MODEL={model!r}): {exc}"
                ) from exc
            logger.warning(
                "LLM startup check failed for %s model (provider=%r, model=%r): %s",
                label,
                provider,
                model,
                exc,
            )


async def _verify_database() -> None:
    """Verify database connectivity at startup.

    Creates the engine and runs a simple SELECT 1 to surface connection
    errors early rather than at first user request.
    """
    engine = get_async_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection verified: %s", engine.url)


def _log_channel_config_warnings() -> None:
    """Warn about channels configured with no allowlist, which denies everyone.

    single_user only: these read the env-var allowlists, which multi_user
    deployments do not use.
    """
    if settings.telegram_bot_token and not settings.telegram_allowed_chat_id:
        logger.warning(
            "No Telegram user ID configured (TELEGRAM_ALLOWED_CHAT_ID). "
            "All messages will be rejected. "
            'Set to "*" to allow all users, or provide a single numeric chat ID.'
        )

    if settings.linq_api_token:
        logger.info("Linq channel enabled (from: %s)", mask_pii(settings.linq_from_number))
        if not settings.linq_allowed_numbers:
            logger.warning(
                "No Linq allowed numbers configured (LINQ_ALLOWED_NUMBERS). "
                "All messages will be rejected. "
                'Set to "*" to allow all, or provide an E.164 phone number.'
            )

    if settings.bluebubbles_server_url:
        logger.info("BlueBubbles channel enabled (server: %s)", settings.bluebubbles_server_url)
        if not settings.bluebubbles_allowed_numbers:
            logger.warning(
                "No BlueBubbles allowed numbers configured (BLUEBUBBLES_ALLOWED_NUMBERS). "
                "All messages will be rejected. "
                'Set to "*" to allow all, or provide an E.164 phone number.'
            )

    if settings.twilio_account_sid and settings.twilio_auth_token:
        sender = (
            f"Messaging Service {settings.twilio_messaging_service_sid}"
            if settings.twilio_messaging_service_sid
            else f"phone {mask_pii(settings.twilio_phone_number) or '<unset>'}"
        )
        logger.info("Twilio channel enabled (sender: %s)", sender)
        if not settings.twilio_api_key_sid or not settings.twilio_api_key_secret:
            logger.warning(
                "Twilio account SID and auth token are set, but "
                "TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET are not. "
                "Inbound webhook signature validation will work, but every "
                "outbound send will fail at runtime. Create a Standard API "
                "key in the Twilio console (Account, API Keys & Tokens) and "
                "set both env vars."
            )
        if not settings.twilio_phone_number and not settings.twilio_messaging_service_sid:
            logger.warning(
                "Twilio credentials are set but neither TWILIO_PHONE_NUMBER "
                "nor TWILIO_MESSAGING_SERVICE_SID is configured. Outbound sends "
                "will fail until one is set."
            )
        if not settings.twilio_allowed_numbers:
            logger.warning(
                "No Twilio allowed numbers configured (TWILIO_ALLOWED_NUMBERS). "
                "All messages will be rejected. "
                'Set to "*" to allow all, or provide an E.164 phone number.'
            )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
    multi_user = settings.auth_mode == "multi_user"

    # Hydrate the settings singleton from persistent storage. The store
    # raises ConfigStoreError if its backend is unreachable (DB down,
    # missing migration, decryption failure) so a misconfigured
    # production environment fails the lifespan loudly rather than
    # booting with empty defaults and crashing 30 lines deeper.
    await _verify_database()
    store = get_settings_store()
    # One-shot migration from the legacy data/config.json into the DB
    # store. No-op once the table has any persistable rows, so safe to
    # leave in place across releases.
    await import_legacy_config_json(store)
    persisted = await store.load()
    applied = apply_to_settings(persisted)
    if applied:
        logger.info(
            "Loaded %d setting(s) from settings store: %s",
            len(applied),
            sorted(applied),
        )

    # Pydantic Settings reads .env for its own declared fields only and
    # does not mutate os.environ. Provider API keys like GROQ_API_KEY are
    # consumed by the any-llm SDK, which reads them directly from
    # os.environ, so we ensure .env values are loaded into the process
    # environment here. Docker Compose already handles this via its
    # env_file directive; this call covers bare-host / local-dev setups.
    load_dotenv()

    await _enforce_single_channel()
    validate_imessage_backend()
    validate_auth_mode()
    log_config_warnings()

    # Warm the Intuit discovery document cache so QuickBooks OAuth
    # endpoints are resolved from the discovery document rather than
    # hardcoded URLs.
    from backend.app.services.oauth import warm_intuit_discovery

    await warm_intuit_discovery()

    await _verify_llm_settings()

    if settings.cors_origins.strip() == "*":
        logger.warning(
            "CORS_ORIGINS is set to '*' (wildcard). "
            "This allows any origin to access your API. "
            "For production, set CORS_ORIGINS to specific origins."
        )

    if multi_user and settings.admin_user_ids:
        logger.warning(
            "ADMIN_USER_IDS is still set (%d entries) but is no longer "
            "consulted: admin is granted exclusively by Subscription.role "
            "in the database. If you've already run "
            "`python -m backend.app.cli promote-env-admins` and verified "
            "the listed users have role='admin' in the DB, this warning is "
            "harmless: remove ADMIN_USER_IDS from your environment to "
            "silence it. If you have NOT run the migration yet, do so now: "
            "until then, the listed users will be denied admin access.",
            len(settings.admin_user_ids),
        )

    if settings.telegram_bot_token:
        if settings.telegram_webhook_secret:
            logger.info("Webhook secret: using explicit TELEGRAM_WEBHOOK_SECRET")
        else:
            logger.info("Webhook secret: auto-derived from bot token")

    # Channel allowlist warnings are single_user only. In multi_user mode
    # senders are approved per tenant through ChannelRoute (see
    # ``channel_route_allowlist``), so the env-var allowlists these warn
    # about are unread and every one of them would be a false alarm.
    if not multi_user:
        _log_channel_config_warnings()

    # Start all channels and the message bus consumer / outbound
    # dispatcher before the heartbeat scheduler, so heartbeat messages
    # published on the bus have somewhere to be delivered.
    manager = get_manager()
    channel_tasks = await manager.start_all()

    # Auto-register channel webhooks against the public base URL. Each
    # channel implements register_paas_webhook() in BaseChannel.
    #
    # Run the per-channel registrations as background tasks rather than
    # awaiting them sequentially. The BlueBubbles registration in
    # particular makes an outbound httpx call to the operator's
    # BlueBubbles server, which can be unreachable for minutes when the
    # user's Mac is asleep. Awaiting it here blocked the lifespan and
    # delayed the first healthcheck-passing response by tens of seconds.
    # Background tasks log success/failure on their own; the lifespan
    # unblocks immediately.
    background_tasks: list[asyncio.Task] = []
    if multi_user:
        # Normalize before comparing, so the health monitor's identical
        # check cannot disagree with this one over a trailing slash and
        # end up verifying a registration that was never made.
        base = settings.app_base_url.rstrip("/")
        if base and base != LOCAL_BASE_URL:
            for channel in manager.channels.values():
                background_tasks.append(
                    asyncio.create_task(
                        _register_channel_webhook_in_background(channel, base),
                        name=f"webhook-register-{channel.name}",
                    )
                )

        # Discover the bot username for the get-started page.
        if settings.telegram_bot_token:
            await discover_bot_username()

    heartbeat_scheduler.start()

    # Background OAuth token refresh: keep tokens fresh proactively so
    # user-facing tool calls do not pay the inline ~150ms refresh cost
    # during the 5 minute pre-expiry window.
    oauth_refresh_scheduler.start()

    if multi_user:
        # Operator monitoring. Started after channels so the BlueBubbles
        # probe can read the channel's reachability flag, and after the
        # LLM/DB verification above so a hard misconfiguration fails the
        # lifespan instead of arriving as an alert email.
        start_alert_flusher()
        health_monitor.start()

    # Notify users whose approval requests were in flight when the previous
    # worker died. Runs after channels are up so outbound delivery works.
    try:
        recovered = await cleanup_orphaned_approvals(message_bus.publish_outbound)
        if recovered:
            logger.info("Recovered %d orphaned approval request(s) on startup", recovered)
    except Exception:
        logger.exception("Orphaned approval cleanup failed on startup")

    # Re-dispatch any inbound messages that were persisted but never ran
    # the agent loop (worker died during the MessageBatcher window).
    # Same shape as the approval cleanup above, runs after channels start.
    try:
        recovered_inbounds = await recover_orphan_inbound_messages()
        if recovered_inbounds:
            logger.info("Re-dispatched %d orphan inbound message(s) on startup", recovered_inbounds)
    except Exception:
        logger.exception("Orphan inbound recovery failed on startup")

    # Retry compaction events stuck in 'pending' (the async LLM call
    # crashed or the process restarted mid-call). The trim watermark is
    # already advanced for these ranges, so without the retry their facts
    # never reach MEMORY.md. Same shape as the recoveries above.
    try:
        retried_compactions = await recover_pending_compactions()
        if retried_compactions:
            logger.info(
                "Completed %d stale pending compaction event(s) on startup",
                retried_compactions,
            )
    except Exception:
        logger.exception("Pending compaction recovery failed on startup")

    # Replay any BlueBubbles iMessages that arrived while Clawbolt was down.
    # The orphan recovery above only handles messages that reached our DB;
    # a webhook delivery that failed because Clawbolt was unreachable
    # leaves no DB row, so we have to ask the BlueBubbles server for them.
    #
    # single_user only. A multi-tenant deployment registers its webhook in
    # the background above and lets the channel's own startup sequence
    # replay, so running the backfill here as well would re-deliver
    # messages that the channel is about to hand over anyway.
    if not multi_user:
        try:
            bb_channel = manager.get("bluebubbles")
            if isinstance(bb_channel, BlueBubblesChannel):
                replayed = await bb_channel.run_startup_backfill()
                if replayed:
                    logger.info(
                        "Replayed %d BlueBubbles message(s) from startup backfill", replayed
                    )
        except KeyError:
            pass
        except Exception:
            logger.exception("BlueBubbles startup backfill failed")

    # Sweep expired media staging rows + on-disk bytes. Steady-state
    # eviction happens inline on stage(), but a crash between cap-enforce
    # and DB commit can leave dead rows past their TTL; this gives every
    # fresh process a clean slate.
    try:
        from backend.app.agent import media_staging

        purged = await media_staging.purge_expired()
        if purged:
            logger.info("Purged %d expired staged media entr(y/ies) on startup", purged)
    except Exception:
        logger.exception("Staged media purge failed on startup")

    yield

    # Cancel any channel start tasks still running.
    for task in channel_tasks:
        if not task.done():
            task.cancel()
    for task in background_tasks:
        if not task.done():
            task.cancel()
    if multi_user:
        health_monitor.stop()
        # Cancelling the flusher triggers one final drain, so errors
        # logged during shutdown (including whatever caused it) still
        # reach the operator.
        stop_alert_flusher()
    await manager.stop_all()
    heartbeat_scheduler.stop()
    oauth_refresh_scheduler.stop()


async def _register_channel_webhook_in_background(channel: BaseChannel, base: str) -> None:
    """Run a channel's PaaS webhook registration without blocking startup.

    Logs success/failure and updates ``channel.webhook_registered`` on
    success. Swallows exceptions so a single channel failure does not
    crash the lifespan task. Bounded by a 30s timeout so a hung server
    doesn't keep the task alive forever.
    """
    try:
        result = await asyncio.wait_for(channel.register_paas_webhook(base), timeout=30.0)
    except TimeoutError:
        logger.warning("%s webhook auto-registration timed out after 30s", channel.name)
        return
    except Exception:
        logger.exception("%s webhook registration raised", channel.name)
        return
    if result is True:
        channel.webhook_registered = True
        logger.info("%s webhook auto-registered", channel.name)
    elif result is False:
        logger.warning("%s webhook auto-registration failed", channel.name)


_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# Paths that automated scanners probe for secrets. The SPA fallback returns
# 404 for these instead of index.html, so the server doesn't look like it
# hosts them.
_BLOCKED_SUFFIXES = (".env", ".pem", ".key", ".pgpass", ".netrc")
_BLOCKED_SEGMENTS = {"credentials", "secrets"}


def create_app() -> FastAPI:
    """Build the ASGI app for the current ``AUTH_MODE``.

    Called once at import to produce the module-level ``app`` that uvicorn
    serves. Exposed as a factory so tests can build a second app under a
    different mode without re-importing this module, whose channel
    registration and agent-hook installation are import-time side effects.
    """
    multi_user = settings.auth_mode == "multi_user"

    # FastAPI's own docs routes are dropped in multi_user mode: /docs is
    # claimed by the SPA's user guide, and without this Swagger UI wins
    # route matching and shadows it.
    app = FastAPI(
        title="Clawbolt",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if multi_user else "/docs",
        redoc_url=None if multi_user else "/redoc",
        openapi_url=None if multi_user else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,  # type: ignore[arg-type]
        allow_origins=settings.cors_origins.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Always installed: it is the source of the per-request correlation ID
    # and the ``X-Request-ID`` response header. The access-log line it also
    # emits is opt-in for a self-host and always on for a hosted
    # deployment, where the operator has no other request-level record.
    app.add_middleware(
        RequestLoggingMiddleware,  # ty: ignore[invalid-argument-type]
        log_timing=settings.log_request_timing or multi_user,
    )

    if multi_user:
        app.add_middleware(SecurityHeadersMiddleware)  # ty: ignore[invalid-argument-type]
        app.add_middleware(SeoMetaMiddleware)  # ty: ignore[invalid-argument-type]
        app.add_middleware(AdminConfigGuardMiddleware)  # ty: ignore[invalid-argument-type]

    app.include_router(health.router, prefix="/api")
    app.include_router(app_config.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(oauth.router, prefix="/api")
    app.include_router(integrations.router, prefix="/api")
    app.include_router(media_temp.router, prefix="/api")

    # Include routers from all registered channels.
    for channel in get_manager().channels.values():
        app.include_router(channel.get_router(), prefix="/api")

    app.include_router(user_profile.router, prefix="/api")
    app.include_router(user_sessions.router, prefix="/api")
    app.include_router(user_memory.router, prefix="/api")
    app.include_router(user_permissions.router, prefix="/api")
    app.include_router(user_tools.router, prefix="/api")
    app.include_router(user_calendar.router, prefix="/api")

    # Hosted-deployment surface: sign-in, the account page, the admin
    # console, operator monitoring, and the public waitlist. Mounted before
    # the SPA fallback below, which otherwise matches every GET.
    if multi_user:
        app.include_router(auth_tokens.router, prefix="/api")
        app.include_router(google_oauth.router, prefix="/api")
        app.include_router(admin.router, prefix="/api")
        app.include_router(admin_shared_data.router, prefix="/api")
        app.include_router(admin_reported_conversations.router, prefix="/api")
        app.include_router(account.router, prefix="/api")
        app.include_router(channels_router.router, prefix="/api")
        app.include_router(monitoring.router, prefix="/api")
        app.include_router(waitlist.router, prefix="/api")

    # -----------------------------------------------------------------
    # Static file serving (built frontend)
    # -----------------------------------------------------------------
    if _FRONTEND_DIST.is_dir():
        # Serve static assets (JS, CSS, images)
        app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa_fallback(request: Request, full_path: str) -> FileResponse:
            """Serve the SPA index.html for all non-API routes."""
            lower = full_path.lower()
            segments = lower.split("/")
            basename = segments[-1] if segments else ""
            if basename.endswith(_BLOCKED_SUFFIXES) or basename.startswith(".env"):
                raise HTTPException(status_code=404)
            if _BLOCKED_SEGMENTS.intersection(segments):
                raise HTTPException(status_code=404)

            file_path = _FRONTEND_DIST / full_path
            resolved = file_path.resolve()
            if resolved.is_file() and resolved.is_relative_to(_FRONTEND_DIST.resolve()):
                return FileResponse(resolved)
            return FileResponse(_FRONTEND_DIST / "index.html")

    return app


app = create_app()
