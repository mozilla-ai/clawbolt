from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.agent.heartbeat import heartbeat_scheduler
from backend.app.config import settings
from backend.app.routers import auth, estimates, health, webhooks


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Start/stop background services."""
    heartbeat_scheduler.start()
    yield
    heartbeat_scheduler.stop()


app = FastAPI(title="Backshop", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(estimates.router, prefix="/api")
