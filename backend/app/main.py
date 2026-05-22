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
from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.agent.inbound_recovery import recover_orphan_inbound_messages
from backend.app.channels import get_manager, register_channel
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
from backend.app.models import ChannelRoute, User
from backend.app.routers import (
    auth,
    health,
    integrations,
    media_temp,
    oauth,
    user_calendar,
    user_memory,
    user_permissions,
    user_profile,
    user_sessions,
    user_tools,
)
from backend.app.services.oauth import oauth_refresh_scheduler

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
# Only the app's own loggers get the configured level; third-party libraries
# (httpcore, httpx, telegram, etc.) stay at WARNING to avoid noise.
logging.getLogger("backend").setLevel(settings.log_level.upper())
logger = logging.getLogger(__name__)


# -- Build and register channels at module scope ----------------------------

register_channel(TelegramChannel(bot_token=settings.telegram_bot_token))
register_channel(WebChatChannel())
register_channel(LinqChannel())
register_channel(BlueBubblesChannel())
register_channel(TwilioChannel())


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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
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
    log_config_warnings()

    # Warm the Intuit discovery document cache so QuickBooks OAuth
    # endpoints are resolved from the discovery document rather than
    # hardcoded URLs.
    from backend.app.services.oauth import warm_intuit_discovery

    await warm_intuit_discovery()

    await _verify_llm_settings()
    heartbeat_scheduler.start()

    # Background OAuth token refresh: keep tokens fresh proactively so
    # user-facing tool calls do not pay the inline ~150ms refresh cost
    # during the 5 minute pre-expiry window.
    oauth_refresh_scheduler.start()

    if settings.telegram_bot_token:
        if settings.telegram_webhook_secret:
            logger.info("Webhook secret: using explicit TELEGRAM_WEBHOOK_SECRET")
        else:
            logger.info("Webhook secret: auto-derived from bot token")

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

    # Start all registered channels concurrently.
    manager = get_manager()
    channel_tasks = await manager.start_all()

    # Notify users whose approval requests were in flight when the previous
    # worker died. Runs after channels are up so outbound delivery works.
    try:
        from backend.app.bus import message_bus

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

    # Replay any BlueBubbles iMessages that arrived while Clawbolt was down.
    # The orphan recovery above only handles messages that reached our DB;
    # a webhook delivery that failed because Clawbolt was unreachable
    # leaves no DB row, so we have to ask the BlueBubbles server for them.
    try:
        bb_channel = manager.get("bluebubbles")
        if isinstance(bb_channel, BlueBubblesChannel):
            replayed = await bb_channel.run_startup_backfill()
            if replayed:
                logger.info("Replayed %d BlueBubbles message(s) from startup backfill", replayed)
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
    await manager.stop_all()
    heartbeat_scheduler.stop()
    oauth_refresh_scheduler.stop()


app = FastAPI(title="Clawbolt", version="0.1.0", lifespan=lifespan)

if settings.log_request_timing:
    from backend.app.middleware.request_logging import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)  # ty: ignore[invalid-argument-type]

app.add_middleware(
    CORSMiddleware,  # type: ignore[arg-type]
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(oauth.router, prefix="/api")
app.include_router(media_temp.router, prefix="/api")

# Include routers from all registered channels.
for _channel in get_manager().channels.values():
    app.include_router(_channel.get_router(), prefix="/api")

app.include_router(user_profile.router, prefix="/api")
app.include_router(user_sessions.router, prefix="/api")
app.include_router(user_memory.router, prefix="/api")
app.include_router(user_permissions.router, prefix="/api")
app.include_router(user_tools.router, prefix="/api")
app.include_router(user_calendar.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")

# ---------------------------------------------------------------------------
# Static file serving (built frontend)
# ---------------------------------------------------------------------------
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    # Paths that automated scanners probe for secrets. Return 404 instead of
    # the SPA index.html so the server doesn't look like it hosts these files.
    _BLOCKED_SUFFIXES = (".env", ".pem", ".key", ".pgpass", ".netrc")
    _BLOCKED_SEGMENTS = {"credentials", "secrets"}

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
