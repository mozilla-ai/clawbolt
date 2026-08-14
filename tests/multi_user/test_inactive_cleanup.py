"""Tests for inactive account cleanup service."""

import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.agent.file_store import UserData, get_user_store
from backend.app.database import db_session_async
from backend.app.models import Subscription, User
from backend.app.services.inactive_cleanup import (
    cleanup_inactive_accounts,
    get_inactive_free_users,
    warn_inactive_users,
)
from tests.multi_user.conftest import open_test_db_session


async def _make_user(
    db: Session,
    user_id: str,
    *,
    plan: str = "free",
    created_days_ago: int = 400,
    warned_days_ago: int | None = None,
) -> UserData:
    """Helper to create a user with subscription."""
    store = get_user_store()
    user = await store.create(user_id=user_id)

    # Backdate created_at directly in the DB (not in updatable fields via store.update)
    now = datetime.datetime.now(datetime.UTC)
    backdated = now - datetime.timedelta(days=created_days_ago)
    oss_db = open_test_db_session()
    try:
        db_user = oss_db.query(User).filter_by(id=user.id).first()
        if db_user is not None:
            db_user.created_at = backdated
            if warned_days_ago is not None:
                warned_at = now - datetime.timedelta(days=warned_days_ago)
                oss_db.execute(
                    text("UPDATE users SET inactivity_warned_at = :warned WHERE id = :uid"),
                    {"warned": warned_at, "uid": user.id},
                )
            oss_db.commit()
    finally:
        oss_db.close()

    user = await store.get_by_id(user.id)  # reload
    assert user is not None

    sub = Subscription(
        user_id=user.id,
        plan=plan,
        status="active",
    )
    db.add(sub)
    db.commit()
    return user


async def _add_conversation(user_id: str, *, days_ago: int = 0) -> None:
    """Helper to create a conversation with a last_message_at timestamp."""
    from backend.app.agent.session_db import get_session_store

    session_store = get_session_store(user_id)
    session, _ = await session_store.get_or_create_session()
    await session_store.add_message(session, direction="inbound", body="test message")

    if days_ago > 0:
        # Backdate the session and message timestamps directly in the DB
        from backend.app.models import ChatSession, Message

        old_ts = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
        oss_db = open_test_db_session()
        try:
            cs = (
                oss_db.query(ChatSession)
                .filter_by(session_id=session.session_id, user_id=user_id)
                .first()
            )
            if cs is not None:
                cs.last_message_at = old_ts
                cs.created_at = old_ts
                # Also backdate messages
                for msg in oss_db.query(Message).filter_by(session_id=cs.id).all():
                    msg.timestamp = old_ts
                oss_db.commit()
        finally:
            oss_db.close()


class TestGetInactiveFreeUsers:
    @pytest.mark.asyncio
    async def test_finds_inactive_users(self, db_session: Session) -> None:
        """Should find free-tier users with no recent activity."""
        inactive_user = await _make_user(db_session, "inactive_1", created_days_ago=400)
        await _make_user(db_session, "active_1", created_days_ago=10)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365)
        async with db_session_async() as adb:
            result = await get_inactive_free_users(adb, cutoff)
        ids = [c.id for c in result]
        assert inactive_user.id in ids

    @pytest.mark.asyncio
    async def test_excludes_pro_users(self, db_session: Session) -> None:
        """Should not include pro-tier users even if inactive."""
        await _make_user(db_session, "pro_1", plan="pro", created_days_ago=400)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365)
        async with db_session_async() as adb:
            result = await get_inactive_free_users(adb, cutoff)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_excludes_recently_active(self, db_session: Session) -> None:
        """Should not include users with recent conversation activity."""
        user = await _make_user(db_session, "recent_1", created_days_ago=400)
        await _add_conversation(user.id, days_ago=0)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365)
        async with db_session_async() as adb:
            result = await get_inactive_free_users(adb, cutoff)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_includes_old_conversation(self, db_session: Session) -> None:
        """Should include users whose last conversation is older than cutoff."""
        user = await _make_user(db_session, "old_conv_1", created_days_ago=400)
        await _add_conversation(user.id, days_ago=370)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365)
        async with db_session_async() as adb:
            result = await get_inactive_free_users(adb, cutoff)
        ids = [c.id for c in result]
        assert user.id in ids

    @pytest.mark.asyncio
    async def test_excludes_deactivated(self, db_session: Session) -> None:
        """Should not include already-deactivated users."""
        store = get_user_store()
        user = await _make_user(db_session, "deactivated_1", created_days_ago=400)
        await store.update(user.id, is_active=False)

        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=365)
        async with db_session_async() as adb:
            result = await get_inactive_free_users(adb, cutoff)
        assert len(result) == 0


class TestWarnInactiveUsers:
    @pytest.mark.asyncio
    async def test_warns_inactive_users(self, db_session: Session) -> None:
        """Should warn users inactive for 11 months but not yet 12."""
        user = await _make_user(db_session, "warn_1", created_days_ago=335)

        async with db_session_async() as adb:
            count = await warn_inactive_users(adb)
        assert count == 1

        # Verify inactivity_warned_at was set (use raw SQL since the column
        # is dynamically added and not a mapped ORM attribute)
        oss_db = open_test_db_session()
        try:
            row = oss_db.execute(
                text("SELECT inactivity_warned_at FROM users WHERE id = :uid"),
                {"uid": user.id},
            ).fetchone()
            assert row is not None
            assert row[0] is not None
        finally:
            oss_db.close()

    @pytest.mark.asyncio
    async def test_skips_users_past_delete_threshold(self, db_session: Session) -> None:
        """Should skip users past 12 months (they'll be deleted instead)."""
        await _make_user(db_session, "old_1", created_days_ago=400)

        async with db_session_async() as adb:
            count = await warn_inactive_users(adb)
        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_already_warned(self, db_session: Session) -> None:
        """Should not re-warn users who have already been warned."""
        await _make_user(db_session, "warned_1", created_days_ago=335, warned_days_ago=10)

        async with db_session_async() as adb:
            count = await warn_inactive_users(adb)
        assert count == 0


class TestCleanupInactiveAccounts:
    @pytest.mark.asyncio
    async def test_deletes_inactive_accounts(self, db_session: Session) -> None:
        """Should delete accounts inactive 12+ months that were warned 30+ days ago."""
        user = await _make_user(db_session, "delete_1", created_days_ago=400, warned_days_ago=35)

        with patch(
            "backend.app.services.inactive_cleanup.delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            async with db_session_async() as adb:
                count = await cleanup_inactive_accounts(adb)

        assert count == 1
        mock_delete.assert_called_once()
        call_args = mock_delete.call_args
        assert call_args[0][1].id == user.id

    @pytest.mark.asyncio
    async def test_skips_unwarned_users(self, db_session: Session) -> None:
        """Should not delete users who haven't been warned first."""
        await _make_user(db_session, "unwarned_1", created_days_ago=400)

        with patch(
            "backend.app.services.inactive_cleanup.delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            async with db_session_async() as adb:
                count = await cleanup_inactive_accounts(adb)

        assert count == 0
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_recently_warned(self, db_session: Session) -> None:
        """Should not delete users warned less than 30 days ago."""
        await _make_user(db_session, "recent_warn_1", created_days_ago=400, warned_days_ago=15)

        with patch(
            "backend.app.services.inactive_cleanup.delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            async with db_session_async() as adb:
                count = await cleanup_inactive_accounts(adb)

        assert count == 0
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_recent_users(self, db_session: Session) -> None:
        """Should not delete users with recent activity."""
        user = await _make_user(db_session, "recent_2", created_days_ago=400, warned_days_ago=35)
        await _add_conversation(user.id, days_ago=0)

        with patch(
            "backend.app.services.inactive_cleanup.delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            async with db_session_async() as adb:
                count = await cleanup_inactive_accounts(adb)

        assert count == 0
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_paid_users(self, db_session: Session) -> None:
        """Should not delete pro/business users even if inactive."""
        await _make_user(db_session, "pro_old", plan="pro", created_days_ago=400)

        with patch(
            "backend.app.services.inactive_cleanup.delete_account",
            new_callable=AsyncMock,
        ) as mock_delete:
            async with db_session_async() as adb:
                count = await cleanup_inactive_accounts(adb)

        assert count == 0
        mock_delete.assert_not_called()
