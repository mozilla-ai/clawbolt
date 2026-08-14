"""Tests for DB-based admin role: auto-promotion, get_current_admin dependency."""

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from backend.app.auth.admin_dep import get_current_admin
from backend.app.auth.oauth_flow import get_or_create_user
from backend.app.config import Settings
from backend.app.models import Subscription, User


class TestAdminAutoPromotion:
    @pytest.mark.asyncio
    async def test_admin_email_case_insensitive(self, async_db: async_sessionmaker) -> None:
        """Mixed-case admin_email should still match after normalization.

        Regression for F-26: the admin_email field_validator lowercases the
        setting value, so ``ADMIN_EMAIL="Admin@Example.com"`` matches the
        incoming ``"admin@example.com"`` from Google.
        """
        google_info = {
            "sub": "admin_sub_case",
            "name": "Admin User",
            "email": "admin@example.com",
        }
        # Use a real Settings instance so the field_validator
        # normalizes admin_email to lowercase. The mock path would
        # bypass the validator, making the test ineffective.
        real_settings = Settings(
            admin_email="Admin@Example.com",  # mixed-case, gets lowercased
            registration_mode="open",
            _env_file=None,
        )
        with patch("backend.app.auth.oauth_flow.settings", real_settings):
            async with async_db() as db:
                user = await get_or_create_user(db, google_info)
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
        assert sub is not None
        assert sub.role == "admin"

    @pytest.mark.asyncio
    async def test_admin_email_match_sets_admin_role(self, async_db: async_sessionmaker) -> None:
        """First login with matching admin_email should set role='admin' on Subscription."""
        google_info = {"sub": "admin_sub", "name": "Admin User", "email": "admin@example.com"}
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.admin_email = "admin@example.com"
            mock_settings.registration_mode = "open"
            async with async_db() as db:
                user = await get_or_create_user(db, google_info)
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
        assert sub is not None
        assert sub.role == "admin"

    @pytest.mark.asyncio
    async def test_non_admin_email_gets_user_role(self, async_db: async_sessionmaker) -> None:
        """First login without matching admin_email should get role='user' on Subscription."""
        google_info = {"sub": "user_sub", "name": "Regular User", "email": "user@example.com"}
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.admin_email = "admin@example.com"
            mock_settings.registration_mode = "open"
            async with async_db() as db:
                user = await get_or_create_user(db, google_info)
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
        assert sub is not None
        assert sub.role == "user"

    @pytest.mark.asyncio
    async def test_empty_admin_email_gets_user_role(self, async_db: async_sessionmaker) -> None:
        """When admin_email is not configured, all users get role='user'."""
        google_info = {"sub": "any_sub", "name": "Any User", "email": "any@example.com"}
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.admin_email = ""
            mock_settings.registration_mode = "open"
            async with async_db() as db:
                user = await get_or_create_user(db, google_info)
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
        assert sub is not None
        assert sub.role == "user"

    @pytest.mark.asyncio
    async def test_no_email_in_google_info_gets_user_role(
        self, async_db: async_sessionmaker
    ) -> None:
        """Missing email in Google user info should default to user role."""
        google_info = {"sub": "noemail_sub", "name": "No Email"}
        with patch("backend.app.auth.oauth_flow.settings") as mock_settings:
            mock_settings.admin_email = "admin@example.com"
            mock_settings.registration_mode = "open"
            async with async_db() as db:
                user = await get_or_create_user(db, google_info)
                sub = (
                    await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                ).scalar_one_or_none()
        assert sub is not None
        assert sub.role == "user"


class TestGetCurrentAdmin:
    """Tests for the async ``get_current_admin`` dependency.

    Setup goes through ``async_db`` rather than the sync ``db_session``
    fixture: the two transactions live on independent connections, so a
    Subscription row inserted via the sync session is invisible to the
    async dependency (READ COMMITTED). See the cross-API caveat in
    ``tests/conftest.py``.
    """

    @pytest.mark.asyncio
    async def test_allows_admin_role(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """User with role='admin' on Subscription should pass the admin check."""
        async with async_db() as db:
            sub = Subscription(
                user_id=async_test_user.id,
                role="admin",
                plan="free",
                status="active",
            )
            db.add(sub)
            await db.commit()

        async with async_db() as db:
            result = await get_current_admin(user=async_test_user, db=db)
        assert result.id == async_test_user.id

    @pytest.mark.asyncio
    async def test_env_var_alone_does_not_grant_admin(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """ADMIN_USER_IDS no longer grants admin at request time.

        Operators must run ``python -m clawbolt_premium promote-env-admins`` to
        migrate these users into ``Subscription.role='admin'``. Until they do
        (or even if they never remove the env var), the env var is ignored;
        admin grants live exclusively in the database.
        """
        async with async_db() as db:
            sub = Subscription(
                user_id=async_test_user.id,
                role="user",
                plan="free",
                status="active",
            )
            db.add(sub)
            await db.commit()

        # Construct a real Settings instance so the property is
        # actually exercised, not a MagicMock that returns truthy by default.
        env_settings = Settings(
            admin_user_ids_raw=async_test_user.user_id,
            _env_file=None,
        )
        assert async_test_user.user_id in env_settings.admin_user_ids  # sanity
        async with async_db() as db:
            with pytest.raises(HTTPException) as exc_info:
                await get_current_admin(user=async_test_user, db=db)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_denies_non_admin(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Non-admin user should get 403."""
        async with async_db() as db:
            sub = Subscription(
                user_id=async_test_user.id,
                role="user",
                plan="free",
                status="active",
            )
            db.add(sub)
            await db.commit()

        async with async_db() as db:
            with pytest.raises(HTTPException) as exc_info:
                await get_current_admin(user=async_test_user, db=db)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_denies_when_no_subscription_row(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """A user with no Subscription row at all (edge case) should get 403,
        not 500. Defends against future code paths that delete subscriptions
        without revoking admin."""
        # Intentionally do not create a Subscription row.
        async with async_db() as db:
            with pytest.raises(HTTPException) as exc_info:
                await get_current_admin(user=async_test_user, db=db)
        assert exc_info.value.status_code == 403


class TestPromoteEnvAdmins:
    """Tests for the one-shot ``python -m clawbolt_premium promote-env-admins``
    migration command. Each branch (promote / already-admin / no-subscription /
    not-found) is exercised against the real DB so the SQL queries are
    actually validated, not just mocked away.
    """

    def _run(
        self,
        db_session: Session,
        admin_user_ids_raw: str,
        capsys: pytest.CaptureFixture[str],
    ) -> str:
        """Run the command with a real Settings and the test session.

        Returns the captured stdout for assertion.

        Patch targets are the *source* modules (``backend.app.config``
        and ``backend.app.database``), not the CLI module. This works
        because ``cmd_promote_env_admins`` does its imports at call time
        (``from backend.app.config import settings`` inside
        the function body), so the function-local lookup picks up the
        patched value. If the function is ever refactored to use a
        top-level import, this patching becomes a no-op. Tests would
        suddenly hit the real settings, and these test cases would need
        the patch targets retargeted to the CLI module.
        """
        from backend.app import cli

        env_settings = Settings(
            admin_user_ids_raw=admin_user_ids_raw,
            _env_file=None,
        )

        from contextlib import asynccontextmanager

        from tests.multi_user.conftest import _SyncToAsyncSessionProxy

        @asynccontextmanager
        async def _yield_session() -> AsyncGenerator[_SyncToAsyncSessionProxy]:
            yield _SyncToAsyncSessionProxy(db_session)

        with (
            patch("backend.app.config.settings", env_settings),
            patch("backend.app.database.db_session_async", _yield_session),
        ):
            cli.cmd_promote_env_admins(MagicMock())
        return capsys.readouterr().out

    def test_promotes_user_with_existing_subscription(
        self,
        test_user: User,
        db_session: Session,
        test_subscription: Subscription,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Happy path: user listed in env var gets ``role='admin'`` set."""
        test_subscription.role = "user"
        db_session.commit()

        out = self._run(db_session, test_user.user_id, capsys)

        db_session.refresh(test_subscription)
        assert test_subscription.role == "admin"
        assert "Promoted to admin: 1" in out
        assert test_user.user_id in out

    def test_idempotent_for_already_admin_user(
        self,
        test_user: User,
        db_session: Session,
        test_subscription: Subscription,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Re-running on a user who is already admin must be a no-op."""
        test_subscription.role = "admin"
        db_session.commit()

        out = self._run(db_session, test_user.user_id, capsys)

        db_session.refresh(test_subscription)
        assert test_subscription.role == "admin"
        assert "Promoted to admin: 0" in out
        assert "Already admin: 1" in out

    def test_skips_unknown_user_id_without_crashing(
        self,
        db_session: Session,
        test_subscription: Subscription,  # ensures fixtures are set up
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A stale env entry for a user that no longer exists must not crash;
        the command should report it and continue."""
        out = self._run(db_session, "google_does_not_exist", capsys)
        assert "Not found in users table (skipped): 1" in out
        assert "google_does_not_exist" in out

    def test_empty_env_var_is_a_noop(
        self,
        db_session: Session,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No env entries → print "nothing to promote" and exit cleanly."""
        out = self._run(db_session, "", capsys)
        assert "nothing to promote" in out

    def test_writes_audit_log_row_on_promotion(
        self,
        test_user: User,
        db_session: Session,
        test_subscription: Subscription,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Each promotion must leave an ``AdminAuditLog`` paper trail.

        Without this, env-var migrations create silent admin grants that
        don't appear in the same audit query operators use to investigate
        UI-driven promotions. ``admin_user_id`` is NULL because there's
        no authenticated admin behind a one-shot CLI; ``admin_email`` is
        a stable marker (``cli:promote-env-admins``) so audit queries can
        distinguish CLI promotions from request-time ones.
        """
        from backend.app.models import AdminAuditLog

        test_subscription.role = "user"
        db_session.commit()

        self._run(db_session, test_user.user_id, capsys)

        rows = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "promote_env_admin")
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.admin_user_id is None
        assert row.admin_email == "cli:promote-env-admins"
        assert row.target_user_id == test_user.id
        assert row.resource_type == "subscription"
        assert row.resource_id == test_user.id
        assert row.detail is not None
        assert row.detail.get("source") == "env_var_migration_cli"
        assert row.detail.get("user_id_external") == test_user.user_id

    def test_does_not_write_audit_log_for_already_admin(
        self,
        test_user: User,
        db_session: Session,
        test_subscription: Subscription,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Idempotent re-runs must not double-log: rows are written for
        actual role flips, not for "already admin" no-ops."""
        from backend.app.models import AdminAuditLog

        test_subscription.role = "admin"
        db_session.commit()

        self._run(db_session, test_user.user_id, capsys)

        rows = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "promote_env_admin")
            .all()
        )
        assert rows == []
