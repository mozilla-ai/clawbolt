"""Database-backed user store.

Replaces the file-based UserStore from the old file_store.py. Uses the User
ORM model for persistence, while keeping UserData Pydantic model as the public
API surface for backward compatibility with premium.

Follows the same SessionLocal() / try-finally pattern used in session_db.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.app.agent.dto import UserData
from backend.app.agent.prompts import load_prompt
from backend.app.config import settings
from backend.app.database import SessionLocal, db_session
from backend.app.models import User

logger = logging.getLogger(__name__)


def provision_user(user: User, db: object | None = None) -> None:
    """Provision a new user: seed DB defaults and create the data directory.

    Seeds soul_text and user_text DB columns with default templates if
    empty. Heartbeat items are NOT seeded here: they start empty and are
    populated when onboarding completes (see ``OnboardingSubscriber``).
    Creates the on-disk data directory for BOOTSTRAP.md and other files
    that still live on the filesystem.
    """
    from sqlalchemy.orm import Session as SASession

    # Seed DB text columns with default templates
    needs_commit = False
    own_session = db is None
    if own_session:
        db = SessionLocal()
    assert isinstance(db, SASession)
    try:
        # Re-query within this session to ensure we can write
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user is not None:
            if not db_user.soul_text:
                db_user.soul_text = f"# Soul\n\n{load_prompt('default_soul')}\n"
                needs_commit = True
            if not db_user.user_text:
                db_user.user_text = f"# User\n\n{load_prompt('default_user')}\n"
                needs_commit = True
            if needs_commit:
                db.commit()
                db.refresh(db_user)
                # Update the in-memory user object so callers see the seeded values
                user.soul_text = db_user.soul_text
                user.user_text = db_user.user_text
    finally:
        if own_session:
            db.close()

    # On-disk: BOOTSTRAP.md + data directory structure
    user_dir = Path(settings.data_dir) / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)

    bootstrap_path = user_dir / "BOOTSTRAP.md"
    if not bootstrap_path.exists() and not user.onboarding_complete:
        bootstrap_path.write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")


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
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


_USER_UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "phone",
        "timezone",
        "preferred_channel",
        "channel_identifier",
        "onboarding_complete",
        "is_active",
        "heartbeat_opt_in",
        "heartbeat_frequency",
        "soul_text",
        "user_text",
        "heartbeat_text",
    }
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
        with db_session() as db:
            user = User(user_id=user_id, **fields)
            db.add(user)
            db.commit()
            db.refresh(user)
            return _user_to_dto(user)

    async def update(self, user_id: str | int, **fields: Any) -> UserData | None:
        """Update a User row by primary key."""
        with db_session() as db:
            user = db.query(User).filter_by(id=str(user_id)).first()
            if user is None:
                return None
            for key, value in fields.items():
                if key in _USER_UPDATABLE_FIELDS:
                    setattr(user, key, value)
            db.commit()
            db.refresh(user)
            return _user_to_dto(user)

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
