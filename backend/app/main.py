from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import settings
from backend.app.routers import auth, health, webhooks

app = FastAPI(title="Backshop", version="0.1.0")

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
