"""Regression: heartbeat fires regardless of Subscription.status.

Premium used to filter users by ``Subscription.status == "active"`` before
delegating to ``run_heartbeat_for_user``. That filter was intentionally
removed: every onboarded, active user gets heartbeats. This test pins the
invariant so a future change cannot silently reintroduce the filter.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.file_store import get_user_store
from backend.app.agent.heartbeat import HeartbeatScheduler
from backend.app.models import ChannelRoute, Subscription


@pytest.mark.asyncio
async def test_canceled_subscription_user_still_evaluated(
    async_db: async_sessionmaker,
) -> None:
    """A user with a canceled subscription must still be evaluated."""
    store = get_user_store()
    user = await store.create_async(
        user_id="google_canceled_regression",
        onboarding_complete=True,
        heartbeat_opt_in=True,
    )
    async with async_db() as db:
        db.add(Subscription(user_id=user.id, plan="pro", status="canceled"))
        db.add(ChannelRoute(user_id=user.id, channel="telegram", channel_identifier="123456"))
        await db.commit()

    fake_route = MagicMock(spec=ChannelRoute)
    fake_route.channel = "telegram"
    fake_route.channel_identifier = "123456"

    scheduler = HeartbeatScheduler()
    with (
        patch(
            "backend.app.agent.heartbeat.resolve_heartbeat_route_async",
            new=AsyncMock(return_value=("telegram", fake_route)),
        ),
        patch(
            "backend.app.agent.heartbeat.run_heartbeat_for_user",
            new_callable=AsyncMock,
        ) as mock_heartbeat,
    ):
        await scheduler.tick()

    mock_heartbeat.assert_called_once()
    assert mock_heartbeat.call_args.kwargs["user"].id == user.id
