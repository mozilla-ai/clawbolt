"""Tests for admin API keys (CLI auth path).

Two surfaces:
* Service: async-only ``mint_api_key`` / ``authenticate_api_key`` /
  ``revoke_api_key``.
* Router: ``GET / POST / DELETE /api/admin/api-keys`` through the
  existing ``TestClient`` HTTP flow.

Service tests use ``async_db`` so mint/auth/revoke all operate on the
same async connection. Route tests seed rows directly where needed so
they stay independent of the service API shape they are exercising
around.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AdminApiKey, Subscription, User
from backend.app.services.admin_api_keys import (
    ACTIVE_KEY_CAP_PER_ADMIN,
    TooManyActiveKeysError,
    authenticate_api_key,
    hash_token,
    is_api_key_token,
    mint_api_key,
    revoke_api_key,
)


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


async def _seed_admin_for_async(async_db: async_sessionmaker) -> User:
    """Insert a User + admin Subscription through the async per-test connection.

    Service-level tests keep their setup inside ``async_db()`` so mint,
    revoke, and auth assertions all share the same savepoint-backed
    async connection.
    """
    user = User(
        id=str(uuid.uuid4()),
        user_id=f"google_async_{uuid.uuid4().hex[:8]}",
        phone="+15555550111",
        channel_identifier="async-admin-channel",
        preferred_channel="telegram",
        onboarding_complete=True,
    )
    sub = Subscription(
        user_id=user.id,
        role="admin",
        plan="free",
        status="active",
    )
    async with async_db() as db:
        # Flush the User first: without an ORM relationship between the two
        # models, one flush orders the INSERTs by mapper sort key and puts
        # subscriptions ahead of users, violating the FK.
        db.add(user)
        await db.flush()
        db.add(sub)
        await db.commit()
        await db.refresh(user)
        await db.refresh(sub)
        db.expunge(user)
        db.expunge(sub)
    return user


def _insert_api_key_row(db: Session, *, owner_user_id: str, label: str) -> AdminApiKey:
    """Insert an admin API-key row directly for route-level setup."""
    cleartext = f"ck_{uuid.uuid4().hex}"
    row = AdminApiKey(
        user_id=owner_user_id,
        label=label,
        key_hash=hash_token(cleartext),
        key_prefix=cleartext[:11],
        revoked_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestServiceLayer:
    def test_is_api_key_token_recognizes_ck_prefix(self) -> None:
        assert is_api_key_token("ck_abc123") is True
        assert is_api_key_token("eyJhbGciOiJIUzI1NiJ9.eyJ...") is False
        assert is_api_key_token("") is False

    async def test_mint_returns_cleartext_once_and_persists_hash(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="laptop")
            assert cleartext.startswith("ck_")
            assert row.user_id == admin.id
            assert row.label == "laptop"
            assert row.key_hash == hash_token(cleartext)
            assert row.key_prefix == cleartext[:11]
            assert row.key_prefix.startswith("ck_")

    async def test_mint_acquires_owner_keyspace_lock(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            with patch(
                "backend.app.services.admin_api_keys._lock_owner_keyspace",
                new_callable=AsyncMock,
            ) as lock_mock:
                await mint_api_key(db, owner_user_id=admin.id, label="laptop")

        lock_mock.assert_awaited_once_with(db, admin.id)

    async def test_mint_truncates_long_label(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, _ = await mint_api_key(db, owner_user_id=admin.id, label="x" * 500)
            assert len(row.label) == 200

    async def test_mint_refuses_when_active_key_cap_reached(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            for i in range(ACTIVE_KEY_CAP_PER_ADMIN):
                await mint_api_key(db, owner_user_id=admin.id, label=f"k{i}")
            with pytest.raises(TooManyActiveKeysError) as ei:
                await mint_api_key(db, owner_user_id=admin.id, label="overflow")
            assert ei.value.active_count == ACTIVE_KEY_CAP_PER_ADMIN
            assert ei.value.cap == ACTIVE_KEY_CAP_PER_ADMIN

    async def test_mint_cap_does_not_count_revoked_keys(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """Revoked keys are excluded from the cap so an admin with a
        long history of rotated keys can still mint. Without this, an
        admin who has rotated keys ten times in a year would be locked
        out forever even though every old key is dead."""
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            rows = []
            for i in range(ACTIVE_KEY_CAP_PER_ADMIN):
                row, _ = await mint_api_key(db, owner_user_id=admin.id, label=f"k{i}")
                rows.append(row)
            assert await revoke_api_key(db, key_id=rows[0].id, owner_user_id=admin.id) is True
            new_row, _ = await mint_api_key(db, owner_user_id=admin.id, label="post-revoke")
            assert new_row.id != rows[0].id

    async def test_authenticate_returns_owner_for_valid_admin_key(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            _, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
        user = await authenticate_api_key(cleartext)
        assert user is not None
        assert user.id == admin.id

    async def test_authenticate_returns_none_for_unknown_token(
        self,
    ) -> None:
        # Even with no rows the lookup must return None rather than raise.
        assert await authenticate_api_key("ck_does-not-exist") is None

    async def test_authenticate_returns_none_for_revoked_key(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
            await revoke_api_key(db, key_id=row.id, owner_user_id=admin.id)
        assert await authenticate_api_key(cleartext) is None

    async def test_authenticate_returns_none_when_owner_not_admin(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """Demoting an admin must invalidate every key they minted."""
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            _, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
            # Demote in DB.
            sub = (
                await db.execute(select(Subscription).where(Subscription.user_id == admin.id))
            ).scalar_one()
            sub.role = "user"
            await db.commit()
        assert await authenticate_api_key(cleartext) is None

    async def test_authenticate_stamps_last_used_at(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
            assert row.last_used_at is None
            row_id = row.id
        await authenticate_api_key(cleartext)
        # Re-read through the async connection to see the update.
        async with async_db() as db:
            refreshed = (
                await db.execute(select(AdminApiKey).where(AdminApiKey.id == row_id))
            ).scalar_one()
            assert refreshed.last_used_at is not None

    async def test_revoke_scopes_to_owner(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, _ = await mint_api_key(db, owner_user_id=admin.id, label="t")
            # Wrong owner: filter rejects; row stays active.
            assert await revoke_api_key(db, key_id=row.id, owner_user_id="wrong-uuid") is False
            assert await revoke_api_key(db, key_id=row.id, owner_user_id=admin.id) is True
            refreshed = (
                await db.execute(select(AdminApiKey).where(AdminApiKey.id == row.id))
            ).scalar_one()
            assert refreshed.revoked_at is not None

    async def test_revoke_is_idempotent(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """Re-revoking an already-revoked key returns True and does not
        bump ``revoked_at``."""
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            row, _ = await mint_api_key(db, owner_user_id=admin.id, label="t")
            assert await revoke_api_key(db, key_id=row.id, owner_user_id=admin.id) is True
            first_revoked = (
                (await db.execute(select(AdminApiKey).where(AdminApiKey.id == row.id)))
                .scalar_one()
                .revoked_at
            )
            assert first_revoked is not None
            assert await revoke_api_key(db, key_id=row.id, owner_user_id=admin.id) is True
            second_revoked = (
                (await db.execute(select(AdminApiKey).where(AdminApiKey.id == row.id)))
                .scalar_one()
                .revoked_at
            )
            assert second_revoked == first_revoked

    async def test_revoke_returns_false_for_unknown_id(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        # Even with no admin row at all the helper must not raise.
        async with async_db() as db:
            assert await revoke_api_key(db, key_id=999_999) is False

    async def test_authenticate_returns_none_for_jwt_shaped_token(
        self,
    ) -> None:
        """A token that does not start with ``ck_`` short-circuits before
        opening a DB session. Pin so a future change that loosens the
        check does not silently start running JWTs through the API-key
        path."""
        assert await authenticate_api_key("eyJhbGciOiJIUzI1NiJ9.fake.fake") is None

    async def test_authenticate_returns_none_when_user_inactive(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """An owner whose ``is_active`` was flipped off cannot auth even
        though the row + admin role still exist."""
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            _, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
            db_user = (await db.execute(select(User).where(User.id == admin.id))).scalar_one()
            db_user.is_active = False
            await db.commit()
        assert await authenticate_api_key(cleartext) is None

    async def test_authenticate_returns_none_when_subscription_missing(
        self,
        async_db: async_sessionmaker,
    ) -> None:
        """A user with an admin key but no Subscription row is treated as
        non-admin. Defends against a partial-state row that could otherwise
        sneak past the role check."""
        admin = await _seed_admin_for_async(async_db)
        async with async_db() as db:
            _, cleartext = await mint_api_key(db, owner_user_id=admin.id, label="t")
            sub = (
                await db.execute(select(Subscription).where(Subscription.user_id == admin.id))
            ).scalar_one()
            await db.delete(sub)
            await db.commit()
        assert await authenticate_api_key(cleartext) is None


class TestRoutes:
    def test_list_returns_admin_own_keys(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        _insert_api_key_row(db_session, owner_user_id=test_user.id, label="laptop")
        _insert_api_key_row(db_session, owner_user_id=test_user.id, label="ci")
        resp = client.get("/api/admin/api-keys")
        assert resp.status_code == 200
        items = resp.json()["items"]
        # Two keys, neither response includes the cleartext token.
        labels = sorted(item["label"] for item in items)
        assert labels == ["ci", "laptop"]
        for item in items:
            assert "token" not in item
            assert item["key_prefix"]

    def test_create_returns_cleartext_once(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        resp = client.post("/api/admin/api-keys", json={"label": "laptop"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"].startswith("ck_")
        assert body["label"] == "laptop"
        assert body["key_prefix"]

        # Re-listing the keys does not re-expose the cleartext.
        list_resp = client.get("/api/admin/api-keys")
        first = list_resp.json()["items"][0]
        assert "token" not in first

    def test_revoke_marks_revoked_and_returns_404_for_unknown(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        mint_resp = client.post("/api/admin/api-keys", json={"label": "t"})
        key_id = mint_resp.json()["id"]

        # First revoke succeeds.
        del_resp = client.delete(f"/api/admin/api-keys/{key_id}")
        assert del_resp.status_code == 200

        # Re-revoking the same id is idempotent (still 200, the row
        # just has revoked_at already set).
        del_resp2 = client.delete(f"/api/admin/api-keys/{key_id}")
        assert del_resp2.status_code == 200

        # Unknown id -> 404.
        ghost = client.delete("/api/admin/api-keys/999999")
        assert ghost.status_code == 404

    def test_create_returns_409_when_active_key_cap_reached(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """At the cap, the route returns 409 with an actionable message
        and does not increment the key count further. Mirror of the
        service-level cap test, exercised through the HTTP surface so a
        future router change that drops the translation is loud."""
        for i in range(ACTIVE_KEY_CAP_PER_ADMIN):
            _insert_api_key_row(db_session, owner_user_id=test_user.id, label=f"k{i}")
        resp = client.post("/api/admin/api-keys", json={"label": "overflow"})
        assert resp.status_code == 409
        # Detail names the cap so a CLI client can show it verbatim.
        assert str(ACTIVE_KEY_CAP_PER_ADMIN) in resp.json()["detail"]

        # The cap rejection did not create a row.
        db_session.expire_all()
        active = (
            db_session.query(AdminApiKey)
            .filter(AdminApiKey.user_id == test_user.id)
            .filter(AdminApiKey.revoked_at.is_(None))
            .count()
        )
        assert active == ACTIVE_KEY_CAP_PER_ADMIN

    def test_revoke_does_not_leak_other_admins_keys(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """An admin should not be able to revoke keys minted by a different
        admin via this endpoint. The scope check is owner_user_id."""
        # Create a second admin and mint a key for them directly.
        from backend.app.models import User as OssUser

        other = OssUser(
            id="other-admin-uuid",
            user_id="google_other_admin",
            phone="",
            onboarding_complete=True,
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            Subscription(
                user_id=other.id,
                role="admin",
                plan="free",
                status="active",
                email="other-admin@example.com",
            )
        )
        db_session.commit()
        row = _insert_api_key_row(db_session, owner_user_id=other.id, label="other-admin-key")

        # The test client is acting as test_user, NOT as other.
        del_resp = client.delete(f"/api/admin/api-keys/{row.id}")
        assert del_resp.status_code == 404

        # And the row is still active (revoked_at unset).
        db_session.expire_all()
        still_active = db_session.query(AdminApiKey).filter(AdminApiKey.id == row.id).first()
        assert still_active is not None
        assert still_active.revoked_at is None
