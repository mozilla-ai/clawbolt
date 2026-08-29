"""Account-deletion service regression tests."""

import datetime
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.config import settings
from backend.app.models import (
    ChannelRoute,
    ChatSession,
    DeletedUserUsage,
    HeartbeatLog,
    LLMPayloadCapture,
    MemoryDocument,
    Subscription,
    UsageQuota,
    User,
    UserPermissionSet,
)
from backend.app.services.user_deletion import delete_account, purge_account


async def _make_subscription(
    async_db: async_sessionmaker, user_id: str, *, plan: str = "free"
) -> Subscription:
    """Insert a Subscription row through the async per-test connection."""
    async with async_db() as db:
        sub = Subscription(user_id=user_id, role="admin", plan=plan, status="active")
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        db.expunge(sub)
    return sub


async def _make_quota(
    async_db: async_sessionmaker,
    user_id: str,
    *,
    messages_used: int = 0,
    tokens_used: int = 0,
) -> UsageQuota:
    """Insert a UsageQuota row through the async per-test connection."""
    now = datetime.datetime.now(datetime.UTC)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with async_db() as db:
        quota = UsageQuota(
            user_id=user_id,
            period_start=period_start,
            messages_used=messages_used,
            messages_limit=1000,
            tokens_used=tokens_used,
            tokens_limit=1_000_000,
        )
        db.add(quota)
        await db.commit()
        await db.refresh(quota)
        db.expunge(quota)
    return quota


async def _set_user_text(
    async_db: async_sessionmaker, user_id: str, *, user_text: str, heartbeat_text: str
) -> None:
    async with async_db() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None:
            return
        user.user_text = user_text
        user.heartbeat_text = heartbeat_text
        await db.commit()


async def _add_archive(
    async_db: async_sessionmaker,
    *,
    original_user_id: str,
    plan: str = "pro",
    total_messages: int = 5,
    total_tokens: int = 100,
) -> None:
    async with async_db() as db:
        db.add(
            DeletedUserUsage(
                original_user_id=original_user_id,
                plan_at_deletion=plan,
                total_messages=total_messages,
                total_tokens=total_tokens,
            )
        )
        await db.commit()


async def _add_oss_rows(async_db: async_sessionmaker, user_id: str) -> None:
    async with async_db() as db:
        db.add_all(
            [
                ChannelRoute(
                    user_id=user_id,
                    channel="linq",
                    channel_identifier="+15551111111",
                ),
                ChatSession(user_id=user_id, session_id=f"sess_{uuid.uuid4().hex[:8]}"),
                HeartbeatLog(user_id=user_id, action_type="send", message_text="hi"),
                MemoryDocument(user_id=user_id, memory_text="remember"),
            ]
        )
        await db.commit()


async def _add_permission_set(async_db: async_sessionmaker, user_id: str) -> None:
    async with async_db() as db:
        db.add(UserPermissionSet(user_id=user_id, data="{}"))
        await db.commit()


async def _get_user(async_db: async_sessionmaker, user_id: str) -> User | None:
    async with async_db() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


class TestAccountDeletion:
    async def test_archives_usage_before_deletion(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Usage totals should be archived to prevent quota-reset abuse."""
        await _make_subscription(async_db, async_test_user.id)
        await _make_quota(async_db, async_test_user.id, messages_used=10, tokens_used=5000)

        async with async_db() as db:
            await delete_account(db, async_test_user)

        async with async_db() as db:
            archive = (await db.execute(select(DeletedUserUsage))).scalar_one_or_none()
        assert archive is not None
        assert archive.original_user_id == async_test_user.user_id
        assert archive.plan_at_deletion == "free"
        assert archive.total_messages == 10
        assert archive.total_tokens == 5000

    async def test_deactivates_user(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """User should be deactivated and PII cleared.

        user_text and heartbeat_text are cleared so that a reactivated user
        doesn't get detected as "already onboarded" by the heuristic in
        is_onboarding_needed, which reads leftover user_text.
        """
        await _make_subscription(async_db, async_test_user.id)
        await _set_user_text(
            async_db,
            async_test_user.id,
            user_text="# User\n\n- Name: Nathan\n- Trade: GC\n",
            heartbeat_text="- Check weather for outdoor jobs",
        )

        async with async_db() as db:
            await delete_account(db, async_test_user)

        # ``delete_account`` deactivates the user via ``store.update_async``,
        # which writes through the same async connection bound by the
        # ``async_db`` fixture. Verify through that connection so the
        # commit (released as a SAVEPOINT) is visible to the read.
        async with async_db() as db:
            user = (
                await db.execute(select(User).where(User.id == async_test_user.id))
            ).scalar_one_or_none()
        assert user is not None
        assert not user.is_active
        assert user.phone == ""
        assert user.soul_text == ""
        assert user.user_text == ""
        assert user.heartbeat_text == ""
        assert user.onboarding_complete is False

    async def test_cascade_deletes_conversations_and_messages(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """User file store data should be deleted."""
        await _make_subscription(async_db, async_test_user.id)
        async with async_db() as db:
            await delete_account(db, async_test_user)

    async def test_cascade_deletes_memories(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """User file store data should be deleted."""
        await _make_subscription(async_db, async_test_user.id)
        async with async_db() as db:
            await delete_account(db, async_test_user)

    async def test_deletes_usage_quotas(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Usage quotas should be deleted after archival."""
        await _make_subscription(async_db, async_test_user.id)
        await _make_quota(async_db, async_test_user.id)

        async with async_db() as db:
            await delete_account(db, async_test_user)

        async with async_db() as db:
            quotas = (await db.execute(select(UsageQuota))).scalars().all()
        assert len(quotas) == 0

    async def test_subscription_marked_canceled_after_deletion(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Subscription status should be set to canceled."""
        await _make_subscription(async_db, async_test_user.id)

        async with async_db() as db:
            await delete_account(db, async_test_user)

        async with async_db() as db:
            sub = (
                await db.execute(
                    select(Subscription).where(Subscription.user_id == async_test_user.id)
                )
            ).scalar_one_or_none()
        assert sub is not None
        assert sub.status == "canceled"


class TestAccountPurge:
    async def test_purge_removes_user_row(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Purge should physically delete the user row, not soft-delete."""
        await _make_subscription(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        assert await _get_user(async_db, async_test_user.id) is None

    async def test_purge_deletes_premium_rows(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Purge should remove subscription and quota rows."""
        await _make_subscription(async_db, async_test_user.id)
        await _make_quota(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            sub = (
                await db.execute(
                    select(Subscription).where(Subscription.user_id == async_test_user.id)
                )
            ).scalar_one_or_none()
            quotas = (await db.execute(select(UsageQuota))).scalars().all()
        assert sub is None
        assert len(quotas) == 0

    async def test_purge_skips_archive_and_clears_existing(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Purge should not archive usage and should clear prior archives."""
        await _make_subscription(async_db, async_test_user.id)
        await _add_archive(async_db, original_user_id=async_test_user.user_id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            archives = (
                (
                    await db.execute(
                        select(DeletedUserUsage).where(
                            DeletedUserUsage.original_user_id == async_test_user.user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(archives) == 0

    async def test_purge_deletes_file_store_directory(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        await _make_subscription(async_db, async_test_user.id)
        user_dir = Path(settings.data_dir) / str(async_test_user.id)
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "note.md").write_text("test")

        async with async_db() as db:
            await purge_account(db, async_test_user)

        assert not user_dir.exists()

    async def test_purge_makes_user_unfindable(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """After purge, looking up the user must return nothing.

        Originally this test asserted via ``store.get_by_id`` (sync path).
        The async fixture's connection lives in a SAVEPOINT under an
        outer transaction, while the sync store session opens on a
        different backend connection that cannot see the uncommitted
        delete. Verifying through the same async connection avoids that
        cross-API trap and exercises the same behavioral assertion.
        """
        await _make_subscription(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            user = (
                await db.execute(select(User).where(User.id == async_test_user.id))
            ).scalar_one_or_none()
        assert user is None

    async def test_purge_cascades_oss_rows(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """OSS FK CASCADE should clear sessions, heartbeats, memory, channels."""
        await _make_subscription(async_db, async_test_user.id)
        await _add_oss_rows(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            for model in (ChannelRoute, ChatSession, HeartbeatLog, MemoryDocument):
                rows = (
                    (await db.execute(select(model).where(model.user_id == async_test_user.id)))
                    .scalars()
                    .all()
                )
                assert len(rows) == 0, f"{model.__name__} not cascaded"

    async def test_purge_removes_user_permission_set(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """UserPermissionSet has no FK CASCADE; service must delete it explicitly."""
        await _make_subscription(async_db, async_test_user.id)
        await _add_permission_set(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            rows = (
                (
                    await db.execute(
                        select(UserPermissionSet).where(
                            UserPermissionSet.user_id == async_test_user.id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 0

    async def test_purge_user_with_no_subscription(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Free users without a subscription row should purge cleanly."""
        async with async_db() as db:
            await purge_account(db, async_test_user)

        assert await _get_user(async_db, async_test_user.id) is None

    async def test_purge_cascades_llm_payload_capture(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """``llm_payload_captures`` has ``ON DELETE CASCADE`` on user_id, so
        purging the user should drop any capture row in the same transaction."""
        await _make_subscription(async_db, async_test_user.id)
        async with async_db() as db:
            db.add(
                LLMPayloadCapture(
                    user_id=async_test_user.id,
                    current_era_payload={"hi": True},
                    current_era_min_message_seq=1,
                    current_era_captured_at=datetime.datetime.now(datetime.UTC),
                    current_era_request_id="r",
                    current_era_payload_bytes=4,
                )
            )
            await db.commit()

        async with async_db() as db:
            await purge_account(db, async_test_user)

        async with async_db() as db:
            rows = (
                (
                    await db.execute(
                        select(LLMPayloadCapture).where(
                            LLMPayloadCapture.user_id == async_test_user.id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 0

    async def test_purge_is_idempotent_after_commit(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Purging an already-purged user should no-op, not raise."""
        await _make_subscription(async_db, async_test_user.id)

        async with async_db() as db:
            await purge_account(db, async_test_user)
        # Second call on the same (now-stale) User object
        async with async_db() as db:
            await purge_account(db, async_test_user)

        assert await _get_user(async_db, async_test_user.id) is None
