import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from any_llm import amessages
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.channels import get_manager, register_channel
from backend.app.channels.telegram import TelegramChannel
from backend.app.channels.webchat import WebChatChannel
from backend.app.config import load_persistent_config, log_config_warnings, settings
from backend.app.database import get_engine
from backend.app.routers import (
    auth,
    health,
    oauth,
    search,
    user_memory,
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
                max_tokens=1,
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
    log_config_warnings()
    await _verify_llm_settings()
    heartbeat_scheduler.start()

    if settings.telegram_bot_token:
        if settings.telegram_webhook_secret:
            logger.info("Webhook secret: using explicit TELEGRAM_WEBHOOK_SECRET")
        else:
            logger.info("Webhook secret: auto-derived from bot token")

    if (
        settings.telegram_bot_token
        and not settings.telegram_allowed_chat_ids
        and not settings.telegram_allowed_usernames
    ):
        logger.warning(
            "No Telegram allowlist configured (TELEGRAM_ALLOWED_CHAT_IDS / "
            "TELEGRAM_ALLOWED_USERNAMES). All messages will be rejected. "
            'Set to "*" to allow all users, or provide a comma-separated list of IDs/usernames.'
        )

    # Start all registered channels concurrently.
    manager = get_manager()
    channel_tasks = await manager.start_all()

    yield

    # Cancel any channel start tasks still running.
    for task in channel_tasks:
        if not task.done():
            task.cancel()
    await manager.stop_all()
    heartbeat_scheduler.stop()


app = FastAPI(title="Clawbolt", version="0.1.0", lifespan=lifespan)

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

# Include routers from all registered channels.
for _channel in get_manager().channels.values():
    app.include_router(_channel.get_router(), prefix="/api")

app.include_router(user_profile.router, prefix="/api")
app.include_router(user_sessions.router, prefix="/api")
app.include_router(user_memory.router, prefix="/api")
app.include_router(user_tools.router, prefix="/api")
app.include_router(search.router, prefix="/api")

# ---------------------------------------------------------------------------
# Static file serving (built frontend)
# ---------------------------------------------------------------------------
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def _spa_fallback(request: Request, full_path: str) -> FileResponse:
        """Serve the SPA index.html for all non-API routes."""
        file_path = _FRONTEND_DIST / full_path
        resolved = file_path.resolve()
        if resolved.is_file() and resolved.is_relative_to(_FRONTEND_DIST.resolve()):
            return FileResponse(resolved)
        return FileResponse(_FRONTEND_DIST / "index.html")
