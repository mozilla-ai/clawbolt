"""Helpers that preserve invariants on ``ChannelRoute`` + ``User`` state.

Both OSS and the premium layer mutate ``ChannelRoute.enabled`` and create
or delete route rows. The invariant that ``User.preferred_channel`` should
point at an enabled non-webchat route (when any exists) is shared between
those write paths, so the helper lives here for both to import.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.app.models import ChannelRoute, User


def _preferred_match_select(user: User) -> Select[tuple[ChannelRoute]]:
    """Builder shared by sync and async realign paths.

    Selects the route that already matches ``user.preferred_channel`` and
    is enabled and not webchat. A non-null result means realignment is a
    no-op.
    """
    return select(ChannelRoute).where(
        ChannelRoute.user_id == user.id,
        ChannelRoute.channel == user.preferred_channel,
        ChannelRoute.enabled.is_(True),
        ChannelRoute.channel != "webchat",
    )


def _fallback_select(user: User) -> Select[tuple[ChannelRoute]]:
    """Builder shared by sync and async realign paths.

    Selects any enabled non-webchat route, used when the current
    ``preferred_channel`` no longer points at one.
    """
    return select(ChannelRoute).where(
        ChannelRoute.user_id == user.id,
        ChannelRoute.enabled.is_(True),
        ChannelRoute.channel != "webchat",
    )


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
    existing = db.execute(_preferred_match_select(user)).scalar_one_or_none()
    if existing is not None:
        return
    fallback = db.execute(_fallback_select(user)).scalar_one_or_none()
    if fallback is not None:
        user.preferred_channel = fallback.channel


async def realign_preferred_channel_async(db: AsyncSession, user: User) -> None:
    """Async peer of :func:`realign_preferred_channel`.

    Same semantics; mirrors the dual-API store pattern (issue #1150) so
    async write paths in OSS and premium can call the helper without
    blocking on a sync session.
    """
    await db.flush()
    existing = (await db.execute(_preferred_match_select(user))).scalar_one_or_none()
    if existing is not None:
        return
    fallback = (await db.execute(_fallback_select(user))).scalar_one_or_none()
    if fallback is not None:
        user.preferred_channel = fallback.channel
