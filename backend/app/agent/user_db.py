"""Database-backed user store.

Replaces the file-based UserStore from the old file_store.py. Uses the User
ORM model for persistence, while keeping UserData Pydantic model as the public
API surface for backward compatibility with premium.

Follows the same SessionLocal() / try-finally pattern used in session_db.py
and client_db.py.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.dto import UserData
from backend.app.database import SessionLocal
from backend.app.models import User

logger = logging.getLogger(__name__)


def _user_to_dto(user: User) -> UserData:
    """Convert a User ORM object to a UserData DTO."""
    return UserData(
        id=user.id,
        user_id=user.user_id,
        phone=user.phone,
        soul_text=user.soul_text,
        user_text=user.user_text,
        heartbeat_text=user.heartbeat_text,
        timezone=user.timezone,
        preferred_channel=user.preferred_channel,
        channel_identifier=user.channel_identifier,
        onboarding_complete=user.onboarding_complete,
        is_active=user.is_active,
        heartbeat_opt_in=user.heartbeat_opt_in,
        heartbeat_frequency=user.heartbeat_frequency,
        folder_scheme=user.folder_scheme,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


class UserStore:
    """Database-backed user storage using User ORM model."""

    async def get_by_id(self, user_id: str | int) -> UserData | None:
        """Look up a user by primary key (id)."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=str(user_id)).first()
            return _user_to_dto(user) if user else None
        finally:
            db.close()

    async def get_by_user_id(self, user_id: str) -> UserData | None:
        """Look up a user by user_id (e.g., 'google_12345')."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(user_id=user_id).first()
            return _user_to_dto(user) if user else None
        finally:
            db.close()

    async def create(self, user_id: str, **fields: Any) -> UserData:
        """Create a new User row and return it as a DTO."""
        db = SessionLocal()
        try:
            user = User(user_id=user_id, **fields)
            db.add(user)
            db.commit()
            db.refresh(user)
            return _user_to_dto(user)
        finally:
            db.close()

    async def update(self, user_id: str | int, **fields: Any) -> UserData | None:
        """Update a User row by primary key."""
        db = SessionLocal()
        try:
            user = db.query(User).filter_by(id=str(user_id)).first()
            if user is None:
                return None
            for key, value in fields.items():
                if hasattr(user, key):
                    setattr(user, key, value)
            db.commit()
            db.refresh(user)
            return _user_to_dto(user)
        finally:
            db.close()

    async def list_all(self) -> list[UserData]:
        """Return all users."""
        db = SessionLocal()
        try:
            users = db.query(User).order_by(User.created_at).all()
            return [_user_to_dto(u) for u in users]
        finally:
            db.close()


_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    """Return the singleton UserStore instance."""
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store


def reset_user_store() -> None:
    """Reset cached store instance. Used by tests."""
    global _user_store
    _user_store = None
