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
from sqlalchemy import text

from backend.app.agent.approval import cleanup_orphaned_approvals
from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.channels import get_manager, register_channel
from backend.app.channels.bluebubbles import BlueBubblesChannel
from backend.app.channels.linq import LinqChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.channels.webchat import WebChatChannel
from backend.app.config import (
    load_persistent_config,
    log_config_warnings,
    settings,
    validate_imessage_backend,
    validate_personal_storage_backend,
)
from backend.app.database import SessionLocal, get_engine
from backend.app.logging_utils import mask_pii
from backend.app.models import ChannelRoute, User
from backend.app.routers import (
    auth,
    health,
    media_temp,
    oauth,
    user_calendar,
    user_memory,
    user_permissions,
    user_profile,
    user_sessions,
    user_tools,
)

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


def _enforce_single_channel() -> None:
    """One-time: disable non-preferred routes for existing multi-channel users.

    After the single-channel refactor, each user should have at most one
    enabled messaging channel.  This cleans up users who had multiple
    channels enabled before the refactor.

    Also realigns ``preferred_channel`` if it points to a channel with no
    enabled route while another enabled route exists. This keeps downstream
    consumers (heartbeat, reauth notifications) consistent without needing
    read-time drift-sync.
    """
    db = SessionLocal()
    try:
        users = db.query(User).all()
        fixed = 0
        for user in users:
            routes = db.query(ChannelRoute).filter_by(user_id=user.id).all()
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
            db.commit()
            logger.info(
                "Single-channel enforcement: fixed %d user(s) with multiple enabled channels",
                fixed,
            )
    finally:
        db.close()


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


def _verify_database() -> None:
    """Verify database connectivity at startup.

    Creates the engine and runs a simple SELECT 1 to surface connection
    errors early rather than at first user request.
    """
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("Database connection verified: %s", engine.url)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
    # Load runtime-configurable settings (Telegram token, allowlists, etc.)
    # from the volume-mounted data/config.json so they survive container
    # restarts. This runs *before* load_dotenv() so that os.environ only
    # contains real env vars at this point; .env values have not yet been
    # injected. Real env vars still take precedence over config.json.
    load_persistent_config()

    # Pydantic Settings reads .env for its own declared fields only and
    # does not mutate os.environ. Provider API keys like GROQ_API_KEY are
    # consumed by the any-llm SDK, which reads them directly from
    # os.environ, so we ensure .env values are loaded into the process
    # environment here. Docker Compose already handles this via its
    # env_file directive; this call covers bare-host / local-dev setups.
    load_dotenv()

    _verify_database()
    _enforce_single_channel()
    validate_imessage_backend()
    validate_personal_storage_backend()
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
    from backend.app.services.oauth import oauth_refresh_scheduler

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

    yield

    # Cancel any channel start tasks still running.
    for task in channel_tasks:
        if not task.done():
            task.cancel()
    await manager.stop_all()
    heartbeat_scheduler.stop()
    from backend.app.services.oauth import oauth_refresh_scheduler

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
