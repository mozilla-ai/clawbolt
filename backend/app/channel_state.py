"""Helpers that preserve invariants on ``ChannelRoute`` + ``User`` state.

Both OSS and the premium layer mutate ``ChannelRoute.enabled`` and create
or delete route rows. The invariant that ``User.preferred_channel`` should
point at an enabled non-webchat route (when any exists) is shared between
those write paths, so the helper lives here for both to import.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.models import ChannelRoute, User


def realign_preferred_channel(db: Session, user: User) -> None:
    """Point ``user.preferred_channel`` at an enabled non-webchat route.

    No-op when ``preferred_channel`` already matches an enabled non-webchat
    route, or when no enabled non-webchat route exists. Write paths that
    disable or delete routes call this so downstream consumers (heartbeat
    routing, reauth notifications) see a consistent view without a
    read-time drift-sync.

    Calls ``db.flush()`` so any just-mutated rows in the session are visible
    to the lookups below. Our ``SessionLocal`` has ``autoflush=False``, so
    without this a route that was disabled or deleted earlier in the same
    transaction would still appear enabled here.
    """
    db.flush()
    existing = (
        db.query(ChannelRoute)
        .filter(
            ChannelRoute.user_id == user.id,
            ChannelRoute.channel == user.preferred_channel,
            ChannelRoute.enabled.is_(True),
            ChannelRoute.channel != "webchat",
        )
        .first()
    )
    if existing is not None:
        return
    fallback = (
        db.query(ChannelRoute)
        .filter(
            ChannelRoute.user_id == user.id,
            ChannelRoute.enabled.is_(True),
            ChannelRoute.channel != "webchat",
        )
        .first()
    )
    if fallback is not None:
        user.preferred_channel = fallback.channel
