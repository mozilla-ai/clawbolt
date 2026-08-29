"""Per-user channel-linking endpoint tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.auth.dependencies import get_current_user
from backend.app.models import ChannelRoute, User


@pytest_asyncio.fixture
async def async_client(
    async_db: async_sessionmaker,
    async_test_user: User,
) -> AsyncGenerator[httpx.AsyncClient]:
    """ASGI-driven async HTTP client wired to the per-test async DB.

    Mirrors the sync ``client`` fixture: overrides auth so requests are
    authenticated as ``async_test_user``, mocks lifespan side effects,
    and yields an ``httpx.AsyncClient`` that talks to the FastAPI app
    in-process via ASGITransport.

    The ``async_db`` fixture rebinds the OSS ``_async_session_factory``
    to a per-test connection, so the route's ``Depends(get_async_db)``
    picks up the same connection setup/verification use here.
    """
    from tests.multi_user.conftest import MULTI_USER_APP as app

    # Sync ``get_db`` override is harmless even though the route uses
    # ``get_async_db`` -- nothing in this router resolves the sync dep,
    # but other code under the same TestClient (e.g. middleware) might.
    app.dependency_overrides[get_current_user] = lambda: async_test_user
    mock_manager = MagicMock()
    mock_manager.start_all = AsyncMock(return_value=[])
    mock_manager.stop_all = AsyncMock()
    settings_store_mock = MagicMock()
    settings_store_mock.load = AsyncMock(return_value={})
    settings_store_mock.save = AsyncMock()
    settings_store_mock.delete = AsyncMock()
    with (
        patch("backend.app.main.get_settings_store", return_value=settings_store_mock),
        patch("backend.app.main.import_legacy_config_json", new_callable=AsyncMock),
        patch("backend.app.main.apply_to_settings", return_value={}),
        patch("backend.app.main.load_dotenv"),
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.main.heartbeat_scheduler"),
        patch("backend.app.main.oauth_refresh_scheduler"),
        patch("backend.app.main.get_manager", return_value=mock_manager),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client

    app.dependency_overrides.pop(get_current_user, None)


async def _add_route(
    async_db: async_sessionmaker,
    *,
    user_id: str,
    channel: str,
    channel_identifier: str,
    enabled: bool = True,
) -> None:
    """Insert a ChannelRoute through the per-test async connection."""
    async with async_db() as db:
        db.add(
            ChannelRoute(
                user_id=user_id,
                channel=channel,
                channel_identifier=channel_identifier,
                enabled=enabled,
            )
        )
        await db.commit()


async def _add_user(
    async_db: async_sessionmaker,
    *,
    user_id: str,
    preferred_channel: str,
) -> None:
    """Insert a User row through the per-test async connection."""
    async with async_db() as db:
        db.add(
            User(
                id=user_id,
                user_id=f"google_{user_id[:8]}",
                phone="",
                channel_identifier="",
                preferred_channel=preferred_channel,
            )
        )
        await db.commit()


async def _get_route(
    async_db: async_sessionmaker, user_id: str, channel: str
) -> ChannelRoute | None:
    """Fetch a single ChannelRoute through the per-test async connection."""
    async with async_db() as db:
        return (
            await db.execute(
                select(ChannelRoute).where(
                    ChannelRoute.user_id == user_id,
                    ChannelRoute.channel == channel,
                )
            )
        ).scalar_one_or_none()


async def _list_routes(
    async_db: async_sessionmaker, user_id: str, channel: str
) -> list[ChannelRoute]:
    async with async_db() as db:
        return list(
            (
                await db.execute(
                    select(ChannelRoute).where(
                        ChannelRoute.user_id == user_id,
                        ChannelRoute.channel == channel,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _get_user(async_db: async_sessionmaker, user_id: str) -> User | None:
    async with async_db() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Telegram linking
# ---------------------------------------------------------------------------


class TestGetTelegramLink:
    """GET /api/channels/telegram"""

    async def test_returns_null_when_not_linked(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.get("/api/channels/telegram")
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_user_id"] is None
        assert data["connected"] is False

    async def test_returns_linked_id(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="telegram",
            channel_identifier="999888777",
        )

        resp = await async_client.get("/api/channels/telegram")
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_user_id"] == "999888777"


class TestSetTelegramLink:
    """PUT /api/channels/telegram"""

    async def test_links_telegram_id(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        resp = await async_client.put(
            "/api/channels/telegram", json={"telegram_user_id": "123456789"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_user_id"] == "123456789"
        assert data["connected"] is True

        route = await _get_route(async_db, async_test_user.id, "telegram")
        assert route is not None
        assert route.channel_identifier == "123456789"

    async def test_updates_existing_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "111111111"})
        resp = await async_client.put(
            "/api/channels/telegram", json={"telegram_user_id": "222222222"}
        )
        assert resp.status_code == 200
        assert resp.json()["telegram_user_id"] == "222222222"

        routes = await _list_routes(async_db, async_test_user.id, "telegram")
        assert len(routes) == 1
        assert routes[0].channel_identifier == "222222222"

    async def test_rejects_empty_id(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/telegram", json={"telegram_user_id": "  "})
        assert resp.status_code == 422

    async def test_rejects_non_numeric_id(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/telegram", json={"telegram_user_id": "abc123"})
        assert resp.status_code == 422
        assert "numeric" in resp.json()["detail"].lower()

    async def test_rejects_duplicate_telegram_id(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        # Create another user with a ChannelRoute claiming the same identifier
        other_id = str(uuid.uuid4())
        await _add_user(async_db, user_id=other_id, preferred_channel="telegram")
        await _add_route(
            async_db,
            user_id=other_id,
            channel="telegram",
            channel_identifier="555555555",
        )

        resp = await async_client.put(
            "/api/channels/telegram", json={"telegram_user_id": "555555555"}
        )
        assert resp.status_code == 409
        assert "already linked" in resp.json()["detail"].lower()

    async def test_allows_same_user_to_re_save_same_id(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "777777777"})
        resp = await async_client.put(
            "/api/channels/telegram", json={"telegram_user_id": "777777777"}
        )
        assert resp.status_code == 200
        assert resp.json()["telegram_user_id"] == "777777777"


class TestRemoveTelegramLink:
    """DELETE /api/channels/telegram"""

    async def test_removes_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "123456789"})
        resp = await async_client.delete("/api/channels/telegram")
        assert resp.status_code == 200
        data = resp.json()
        assert data["telegram_user_id"] is None
        assert data["connected"] is False

        route = await _get_route(async_db, async_test_user.id, "telegram")
        assert route is None

    async def test_remove_when_not_linked_is_ok(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.delete("/api/channels/telegram")
        assert resp.status_code == 200
        assert resp.json()["telegram_user_id"] is None


# ---------------------------------------------------------------------------
# Linq (iMessage / RCS / SMS) linking
# ---------------------------------------------------------------------------


class TestGetLinqLink:
    """GET /api/channels/linq"""

    async def test_returns_null_when_not_linked(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.get("/api/channels/linq")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

    async def test_returns_linked_phone(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        resp = await async_client.get("/api/channels/linq")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"


class TestSetLinqLink:
    """PUT /api/channels/linq"""

    async def test_links_phone_number(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15551234567"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"
        assert data["connected"] is True

        route = await _get_route(async_db, async_test_user.id, "linq")
        assert route is not None
        assert route.channel_identifier == "+15551234567"

    async def test_updates_existing_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/linq", json={"phone_number": "+15551111111"})
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15552222222"})
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15552222222"

        routes = await _list_routes(async_db, async_test_user.id, "linq")
        assert len(routes) == 1
        assert routes[0].channel_identifier == "+15552222222"

    async def test_rejects_empty_phone(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "  "})
        assert resp.status_code == 422

    async def test_rejects_invalid_format(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "5551234567"})
        assert resp.status_code == 422
        assert "E.164" in resp.json()["detail"]

    async def test_rejects_non_numeric_after_plus(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+1555abc4567"})
        assert resp.status_code == 422
        assert "E.164" in resp.json()["detail"]

    async def test_rejects_duplicate_phone_number(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = str(uuid.uuid4())
        await _add_user(async_db, user_id=other_id, preferred_channel="linq")
        await _add_route(
            async_db,
            user_id=other_id,
            channel="linq",
            channel_identifier="+15559999999",
        )

        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15559999999"})
        assert resp.status_code == 409
        assert "already linked" in resp.json()["detail"].lower()

    async def test_allows_same_user_to_re_save_same_phone(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        await async_client.put("/api/channels/linq", json={"phone_number": "+15557777777"})
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15557777777"})
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15557777777"


class TestRemoveLinqLink:
    """DELETE /api/channels/linq"""

    async def test_removes_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/linq", json={"phone_number": "+15551234567"})
        resp = await async_client.delete("/api/channels/linq")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

        route = await _get_route(async_db, async_test_user.id, "linq")
        assert route is None

    async def test_remove_when_not_linked_is_ok(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.delete("/api/channels/linq")
        assert resp.status_code == 200
        assert resp.json()["phone_number"] is None


# ---------------------------------------------------------------------------
# BlueBubbles (iMessage via self-hosted Mac bridge) linking
# ---------------------------------------------------------------------------


class TestGetBlueBubblesLink:
    """GET /api/channels/bluebubbles"""

    async def test_returns_null_when_not_linked(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.get("/api/channels/bluebubbles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

    async def test_returns_linked_phone(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="bluebubbles",
            channel_identifier="+15551234567",
        )

        resp = await async_client.get("/api/channels/bluebubbles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"

    async def test_returns_linked_email(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="bluebubbles",
            channel_identifier="user@icloud.com",
        )

        resp = await async_client.get("/api/channels/bluebubbles")
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "user@icloud.com"


class TestSetBlueBubblesLink:
    """PUT /api/channels/bluebubbles"""

    async def test_links_phone_number(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "+15551234567"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"
        assert data["connected"] is True

        route = await _get_route(async_db, async_test_user.id, "bluebubbles")
        assert route is not None
        assert route.channel_identifier == "+15551234567"

    async def test_links_email(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "user@icloud.com"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "user@icloud.com"
        assert data["connected"] is True

    async def test_updates_existing_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/bluebubbles", json={"phone_number": "+15551111111"})
        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "+15552222222"}
        )
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15552222222"

        routes = await _list_routes(async_db, async_test_user.id, "bluebubbles")
        assert len(routes) == 1
        assert routes[0].channel_identifier == "+15552222222"

    async def test_rejects_empty_identifier(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/bluebubbles", json={"phone_number": "  "})
        assert resp.status_code == 422

    async def test_rejects_invalid_format(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "not-valid"}
        )
        assert resp.status_code == 422
        assert "E.164" in resp.json()["detail"]

    async def test_rejects_duplicate_identifier(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = str(uuid.uuid4())
        await _add_user(async_db, user_id=other_id, preferred_channel="bluebubbles")
        await _add_route(
            async_db,
            user_id=other_id,
            channel="bluebubbles",
            channel_identifier="+15559999999",
        )

        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "+15559999999"}
        )
        assert resp.status_code == 409
        assert "already linked" in resp.json()["detail"].lower()

    async def test_allows_same_user_to_re_save(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        await async_client.put("/api/channels/bluebubbles", json={"phone_number": "+15557777777"})
        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "+15557777777"}
        )
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15557777777"


class TestRemoveBlueBubblesLink:
    """DELETE /api/channels/bluebubbles"""

    async def test_removes_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/bluebubbles", json={"phone_number": "+15551234567"})
        resp = await async_client.delete("/api/channels/bluebubbles")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

        route = await _get_route(async_db, async_test_user.id, "bluebubbles")
        assert route is None

    async def test_remove_when_not_linked_is_ok(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.delete("/api/channels/bluebubbles")
        assert resp.status_code == 200
        assert resp.json()["phone_number"] is None


# ---------------------------------------------------------------------------
# Single-channel enforcement (link auto-disables siblings)
# ---------------------------------------------------------------------------


class TestSingleChannelEnforcement:
    """Linking a channel auto-disables other non-webchat routes."""

    async def test_link_telegram_disables_linq(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        resp = await async_client.put(
            "/api/channels/telegram", json={"telegram_user_id": "123456789"}
        )
        assert resp.status_code == 200

        linq_route = await _get_route(async_db, async_test_user.id, "linq")
        assert linq_route is not None
        assert linq_route.enabled is False

    async def test_link_linq_disables_telegram(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="telegram",
            channel_identifier="999888777",
        )

        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15551234567"})
        assert resp.status_code == 200

        tg_route = await _get_route(async_db, async_test_user.id, "telegram")
        assert tg_route is not None
        assert tg_route.enabled is False

    async def test_link_preserves_webchat_route(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="webchat",
            channel_identifier=async_test_user.id,
        )

        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "123456789"})

        webchat_route = await _get_route(async_db, async_test_user.id, "webchat")
        assert webchat_route is not None
        assert webchat_route.enabled is True

    async def test_link_updates_preferred_channel(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        # async_test_user starts with preferred_channel="telegram"
        resp = await async_client.put("/api/channels/linq", json={"phone_number": "+15551234567"})
        assert resp.status_code == 200

        user = await _get_user(async_db, async_test_user.id)
        assert user is not None
        assert user.preferred_channel == "linq"

    async def test_unlink_does_not_reenable_others(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "123456789"})

        resp = await async_client.delete("/api/channels/telegram")
        assert resp.status_code == 200

        linq_route = await _get_route(async_db, async_test_user.id, "linq")
        assert linq_route is not None
        assert linq_route.enabled is False

    async def test_unlink_preferred_repoints_to_other_enabled(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Removing the preferred channel's link realigns ``preferred_channel``
        to another enabled non-webchat route.

        The OSS heartbeat lookup is a pure read, so the write path must own
        this invariant. Without realignment, ``preferred_channel`` would
        point at a nonexistent route until the next inbound message.
        """
        # Set up: telegram is preferred and linked; linq is also enabled.
        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "999888777"})
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )
        user = await _get_user(async_db, async_test_user.id)
        assert user is not None
        assert user.preferred_channel == "telegram"

        resp = await async_client.delete("/api/channels/telegram")
        assert resp.status_code == 200

        user = await _get_user(async_db, async_test_user.id)
        assert user is not None
        assert user.preferred_channel == "linq"

    async def test_unlink_non_preferred_leaves_preferred_alone(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Removing a non-preferred link does not change ``preferred_channel``."""
        await async_client.put("/api/channels/telegram", json={"telegram_user_id": "999888777"})
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
            enabled=False,
        )

        resp = await async_client.delete("/api/channels/linq")
        assert resp.status_code == 200

        user = await _get_user(async_db, async_test_user.id)
        assert user is not None
        assert user.preferred_channel == "telegram"

    async def test_link_bluebubbles_disables_telegram_and_linq(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="telegram",
            channel_identifier="999888777",
        )
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15559999999",
        )

        resp = await async_client.put(
            "/api/channels/bluebubbles", json={"phone_number": "+15551234567"}
        )
        assert resp.status_code == 200

        tg = await _get_route(async_db, async_test_user.id, "telegram")
        linq = await _get_route(async_db, async_test_user.id, "linq")
        assert tg is not None and tg.enabled is False
        assert linq is not None and linq.enabled is False

        user = await _get_user(async_db, async_test_user.id)
        assert user is not None
        assert user.preferred_channel == "bluebubbles"


# ---------------------------------------------------------------------------
# Twilio (RCS via Messaging Service, with SMS/MMS fallback)
# ---------------------------------------------------------------------------


class TestGetTwilioLink:
    """GET /api/channels/twilio"""

    async def test_returns_null_when_not_linked(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.get("/api/channels/twilio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

    async def test_returns_linked_phone(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="twilio",
            channel_identifier="+15551234567",
        )

        resp = await async_client.get("/api/channels/twilio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"
        assert data["connected"] is True


class TestSetTwilioLink:
    """PUT /api/channels/twilio"""

    async def test_links_phone_number(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "+15551234567"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] == "+15551234567"
        assert data["connected"] is True

        route = await _get_route(async_db, async_test_user.id, "twilio")
        assert route is not None
        assert route.channel_identifier == "+15551234567"

    async def test_updates_existing_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/twilio", json={"phone_number": "+15551111111"})
        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "+15552222222"})
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15552222222"

        routes = await _list_routes(async_db, async_test_user.id, "twilio")
        assert len(routes) == 1
        assert routes[0].channel_identifier == "+15552222222"

    async def test_rejects_empty_identifier(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "  "})
        assert resp.status_code == 422

    async def test_rejects_invalid_format(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "not-valid"})
        assert resp.status_code == 422
        assert "E.164" in resp.json()["detail"]

    async def test_rejects_email(self, async_client: httpx.AsyncClient) -> None:
        """Twilio is phone-only -- iCloud emails are a BlueBubbles thing."""
        resp = await async_client.put(
            "/api/channels/twilio", json={"phone_number": "user@icloud.com"}
        )
        assert resp.status_code == 422
        assert "E.164" in resp.json()["detail"]

    async def test_rejects_duplicate_identifier(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
    ) -> None:
        other_id = str(uuid.uuid4())
        await _add_user(async_db, user_id=other_id, preferred_channel="twilio")
        await _add_route(
            async_db,
            user_id=other_id,
            channel="twilio",
            channel_identifier="+15559999999",
        )

        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "+15559999999"})
        assert resp.status_code == 409
        assert "already linked" in resp.json()["detail"].lower()

    async def test_allows_same_user_to_re_save(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        await async_client.put("/api/channels/twilio", json={"phone_number": "+15557777777"})
        resp = await async_client.put("/api/channels/twilio", json={"phone_number": "+15557777777"})
        assert resp.status_code == 200
        assert resp.json()["phone_number"] == "+15557777777"


class TestRemoveTwilioLink:
    """DELETE /api/channels/twilio"""

    async def test_removes_link(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await async_client.put("/api/channels/twilio", json={"phone_number": "+15551234567"})
        resp = await async_client.delete("/api/channels/twilio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone_number"] is None
        assert data["connected"] is False

        route = await _get_route(async_db, async_test_user.id, "twilio")
        assert route is None

    async def test_remove_when_not_linked_is_ok(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.delete("/api/channels/twilio")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Welcome-text kickoff
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _clear_welcome_cooldown() -> AsyncGenerator[None]:
    """Reset the per-(user, channel) cooldown dict between tests.

    The endpoint guards against double-clicks via an in-memory ``dict``;
    leaving entries behind from a prior test would falsely 429 the next
    one. Snapshot-and-restore around each test keeps the module state
    clean without exposing the dict from the router module's public API.
    """
    from backend.app.routers import channels as channels_router

    saved = dict(channels_router._last_welcome_at)
    channels_router._last_welcome_at.clear()
    try:
        yield
    finally:
        channels_router._last_welcome_at.clear()
        channels_router._last_welcome_at.update(saved)


class TestSendLinqWelcome:
    """POST /api/channels/linq/welcome"""

    async def test_sends_welcome_text(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(return_value="msg-123")

        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            resp = await async_client.post("/api/channels/linq/welcome")

        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] is True
        assert data["channel"] == "linq"
        assert data["channel_identifier"] == "+15551234567"

        mock_channel.send_text.assert_awaited_once()
        await_args = mock_channel.send_text.await_args
        assert await_args is not None
        assert await_args.kwargs["to"] == "+15551234567"
        assert "Clawbolt" in await_args.kwargs["body"]
        assert "Reply" in await_args.kwargs["body"]

    async def test_persists_outbound_in_session_history(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        """The agent needs to see the welcome text as prior context on first reply.

        Reads through the SessionStore (the same store the endpoint writes
        through) rather than a fresh raw SQL session: the endpoint persists
        via ``db_session_async()`` which under the ``async_db`` fixture
        binds to the per-test connection's outer transaction. Reading via
        the same store keeps both writer and reader on the rebound
        ``_async_session_factory`` path the per-test connection owns.
        """
        from backend.app.agent.session_db import get_session_store

        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(return_value="msg-123")
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            resp = await async_client.post("/api/channels/linq/welcome")
        assert resp.status_code == 200

        store = get_session_store(async_test_user.id)
        session, _ = await store.get_or_create_session_async()
        assert len(session.messages) == 1
        assert session.messages[0].direction == "outbound"
        assert "Clawbolt" in session.messages[0].body
        assert session.channel == "linq"

    async def test_404_when_no_route(
        self,
        async_client: httpx.AsyncClient,
        _clear_welcome_cooldown: None,
    ) -> None:
        resp = await async_client.post("/api/channels/linq/welcome")
        assert resp.status_code == 404
        assert "linked" in resp.json()["detail"].lower()

    async def test_502_when_send_text_raises(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(side_effect=RuntimeError("provider blew up"))
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            resp = await async_client.post("/api/channels/linq/welcome")

        assert resp.status_code == 502
        assert "text us yourself" in resp.json()["detail"].lower()

    async def test_503_when_channel_not_registered(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        with patch(
            "backend.app.routers.channels.get_channel",
            side_effect=KeyError("linq"),
        ):
            resp = await async_client.post("/api/channels/linq/welcome")

        assert resp.status_code == 503

    async def test_failed_send_does_not_burn_cooldown(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        """A 502 must leave the cooldown unstamped so the user can retry.

        Pins the invariant that the cooldown only stamps on successful
        delivery. A future refactor that moves the timestamp write
        earlier (or wraps it around the send) would silently make
        provider hiccups burn the user's one chance to retry.
        """
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(side_effect=RuntimeError("provider blew up"))
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            first = await async_client.post("/api/channels/linq/welcome")
            # Second call must not hit the cooldown (no stamp on failure).
            mock_channel.send_text.side_effect = None
            mock_channel.send_text.return_value = "msg-1"
            second = await async_client.post("/api/channels/linq/welcome")

        assert first.status_code == 502
        assert second.status_code == 200

    async def test_429_within_cooldown(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="linq",
            channel_identifier="+15551234567",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(return_value="msg-1")
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            first = await async_client.post("/api/channels/linq/welcome")
            second = await async_client.post("/api/channels/linq/welcome")

        assert first.status_code == 200
        assert second.status_code == 429
        # Second click must not actually hit the provider.
        assert mock_channel.send_text.await_count == 1


class TestSendBlueBubblesWelcome:
    """POST /api/channels/bluebubbles/welcome"""

    async def test_sends_to_linked_identifier(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="bluebubbles",
            channel_identifier="user@icloud.com",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(return_value="guid-1")
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            resp = await async_client.post("/api/channels/bluebubbles/welcome")

        assert resp.status_code == 200
        assert resp.json()["channel_identifier"] == "user@icloud.com"
        mock_channel.send_text.assert_awaited_once()


class TestSendTwilioWelcome:
    """POST /api/channels/twilio/welcome"""

    async def test_sends_to_linked_number(
        self,
        async_client: httpx.AsyncClient,
        async_db: async_sessionmaker,
        async_test_user: User,
        _clear_welcome_cooldown: None,
    ) -> None:
        await _add_route(
            async_db,
            user_id=async_test_user.id,
            channel="twilio",
            channel_identifier="+15558675309",
        )

        mock_channel = MagicMock()
        mock_channel.send_text = AsyncMock(return_value="SMxxxx")
        with patch(
            "backend.app.routers.channels.get_channel",
            return_value=mock_channel,
        ):
            resp = await async_client.post("/api/channels/twilio/welcome")

        assert resp.status_code == 200
        assert resp.json()["channel"] == "twilio"
        assert resp.json()["channel_identifier"] == "+15558675309"
