"""Pick the channel a proactive message goes out on.

A heartbeat has no inbound message to reply to, so it has to choose a route
itself. Channels the user must be actively connected to (web chat) cannot
receive a push and are excluded.
"""

from __future__ import annotations

import logging

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.channels import get_channel
from backend.app.models import ChannelRoute, User

logger = logging.getLogger(__name__)

# Channels that cannot deliver proactive (push) messages because the user
# must be actively connected to receive them.
_NON_PUSHABLE_CHANNELS: frozenset[str] = frozenset({"webchat"})


def _heartbeat_route_select(user: User) -> Select[tuple[ChannelRoute]]:
    """Select the enabled, pushable routes for *user*, newest first."""
    return select(ChannelRoute).where(
        ChannelRoute.user_id == user.id,
        ChannelRoute.enabled.is_(True),
        ChannelRoute.channel.notin_(list(_NON_PUSHABLE_CHANNELS)),
    )


def _check_route_is_registered(user: User, route: ChannelRoute) -> tuple[str, ChannelRoute] | None:
    """Verify the channel is registered in this process and return the result tuple.

    Kept apart from the query so the post-query branching is testable
    without a database.
    """
    try:
        get_channel(route.channel)
    except KeyError:
        logger.debug(
            "Heartbeat skipped for user %s: channel %s not registered",
            user.id,
            route.channel,
        )
        return None
    return route.channel, route


async def resolve_heartbeat_route_async(
    user: User,
    db: AsyncSession,
) -> tuple[str, ChannelRoute] | None:
    """Resolve the channel a proactive message should go out on."""
    route = (await db.execute(_heartbeat_route_select(user))).scalar_one_or_none()

    if route is None:
        logger.debug(
            "Heartbeat skipped for user %s: no pushable route configured",
            user.id,
        )
        return None

    return _check_route_is_registered(user, route)
