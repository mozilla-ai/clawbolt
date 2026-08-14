"""Tests for the structured admin audit log layer (issue #325 work item 1).

PR #324 already shipped the ``admin_audit_logs`` table + an inline
``_write_admin_audit()`` helper for 3 PII-dense reads. This work item
augments that with full coverage: all 20 admin endpoints write a row,
including 404 paths.

These tests cover the new pieces. The original ``test_writes_audit_log_on_each_call``
tests in ``test_admin_router.py`` still cover the existing endpoints, and
``TestAdminAuditFailOpen`` continues to verify reads succeed when the
audit commit raises.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.database import get_async_db
from backend.app.middleware.rate_limit import _auth_rate_limiter
from backend.app.models import AdminAuditLog, ChatSession, ReportedConversation, Subscription, User
from tests.multi_user.conftest import open_test_db_session


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> None:
    _auth_rate_limiter.reset()


def _create_user_in_db(**overrides: object) -> User:
    defaults = {
        "id": str(uuid.uuid4()),
        "user_id": f"google_{uuid.uuid4().hex[:8]}",
        "phone": "",
        "onboarding_complete": True,
    }
    defaults.update(overrides)
    db = open_test_db_session()
    try:
        user = User(**defaults)
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    return user


class TestAuditOnMutation:
    """Mutations must write a row regardless of outcome."""

    def test_deactivate_writes_audit_row(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        target = _create_user_in_db(is_active=True)

        before = db_session.query(AdminAuditLog).count()
        resp = client.post(f"/api/admin/users/{target.id}/deactivate")
        assert resp.status_code == 200

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1
        latest = rows[0]
        assert latest.action == "deactivate_user"
        assert latest.target_user_id == target.id
        assert latest.resource_type == "user"

    def test_404_still_writes_audit_row(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Audit row must exist even when the route 404s.

        Forensic queries care about who *attempted* a read or mutation,
        not just who succeeded.
        """
        before = db_session.query(AdminAuditLog).count()
        resp = client.post(f"/api/admin/users/{uuid.uuid4()}/deactivate")
        assert resp.status_code == 404

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1
        assert rows[0].action == "deactivate_user"


# Every user-scoped admin route that 404s on a missing user. This list
# catches a real bug we hit once: setting ``ctx.target_user_id`` from the
# URL parameter BEFORE the existence check causes the audit insert to
# fail the FK on ``users.id`` and silently drops the row. The test below
# exercises every such route to make that regression surface in CI.
_USER_SCOPED_ROUTES_THAT_404 = [
    ("GET", "/api/admin/users/{uid}", "view_user_detail"),
    ("GET", "/api/admin/users/{uid}/heartbeat-logs", "view_heartbeat_logs"),
    ("GET", "/api/admin/users/{uid}/llm-usage-logs", "view_llm_usage_logs"),
    ("GET", "/api/admin/users/{uid}/staged-media", "view_staged_media"),
    ("GET", "/api/admin/users/{uid}/webhook-events", "view_webhook_events"),
    ("GET", "/api/admin/usage/{uid}", "view_usage"),
    ("POST", "/api/admin/users/{uid}/activate", "activate_user"),
    ("POST", "/api/admin/users/{uid}/deactivate", "deactivate_user"),
    ("POST", "/api/admin/users/{uid}/reset-quota", "reset_quota"),
    ("DELETE", "/api/admin/users/{uid}", "delete_user"),
]


@pytest.mark.parametrize(
    ("method", "path_template", "expected_action"), _USER_SCOPED_ROUTES_THAT_404
)
class TestRoute404WritesAuditRow:
    """Regression guard: every user-scoped route 404s cleanly AND writes an audit row.

    Trips loudly if a contributor reintroduces the FK-violation bug by
    setting ``ctx.target_user_id`` from the URL parameter before the user
    existence check.
    """

    def test_route_404s_and_records_attempt(
        self,
        method: str,
        path_template: str,
        expected_action: str,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        bogus_uid = str(uuid.uuid4())
        path = path_template.format(uid=bogus_uid)

        before = db_session.query(AdminAuditLog).count()
        resp = client.request(method, path)
        assert resp.status_code == 404, f"{method} {path} expected 404, got {resp.status_code}"

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1, (
            f"{method} {path} did not write an audit row — likely the "
            f"FK regression on target_user_id"
        )
        latest = rows[0]
        assert latest.action == expected_action
        # target_user_id stays None on 404 paths (FK requires real users.id);
        # the attempted UUID lives in resource_id for forensic reconstruction.
        assert latest.target_user_id is None
        assert latest.resource_id == bogus_uid


class TestAuthSourceOnAuditRow:
    """Forensic queries can answer "which actions came in via the CLI?"
    by filtering on ``detail->>'auth_source'``.

    The auth path stamps ``request.state.auth_source`` to ``"api_key"``
    or ``"session"`` so the audit dependency can record it without
    re-parsing the Authorization header. Tests exercise both the
    bottom (commit picks up the field) and top (the auth path stamps
    it) of the chain.
    """

    def test_session_auth_lands_session_auth_source_in_detail(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """The conftest ``client`` fixture overrides
        ``get_current_user`` so it bypasses ``resolve_multi_user``
        and never sets ``request.state.auth_source``. The audit dep
        must default to ``"session"`` so existing browser-driven audit
        rows look right."""
        # Any audited admin route works; pick a cheap one.
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200

        db_session.commit()
        latest = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).first()
        assert latest is not None
        assert latest.detail is not None
        assert latest.detail.get("auth_source") == "session"

    async def test_resolver_stamps_api_key_source(self) -> None:
        """The auth path must stamp ``request.state.auth_source`` to
        ``"api_key"`` when the bearer token starts with ``ck_``. A unit
        test against the dependency itself, since the conftest fixture
        bypasses it."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from starlette.datastructures import State

        from backend.app.auth.session_auth import resolve_multi_user

        request = MagicMock()
        request.state = State()
        request.headers = {"Authorization": "Bearer ck_test123"}
        sentinel_user = User(id=str(uuid.uuid4()), user_id="ck-user", phone="")

        # authenticate_api_key is async (issue #396), so the patch
        # must return an awaitable. AsyncMock resolves to the
        # supplied user when awaited.
        with patch(
            "backend.app.auth.session_auth.authenticate_api_key",
            new=AsyncMock(return_value=sentinel_user),
        ):
            result = await resolve_multi_user(request)
        assert result is sentinel_user
        assert request.state.auth_source == "api_key"

    async def test_resolver_stamps_session_source_for_jwt(self) -> None:
        """JWT path stamps ``"session"``. Mirror of the api_key test
        above on the other branch of the auth dispatch.

        After #429 the JWT branch reads through ``db_session_async`` (an
        async context manager), so the patch shape is async: the manager
        yields a session whose ``execute`` is awaitable and whose
        ``scalar_one_or_none`` returns the sentinel user.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from starlette.datastructures import State

        from backend.app.auth.session_auth import resolve_multi_user

        request = MagicMock()
        request.state = State()
        request.headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.fake.fake"}
        sentinel_user = User(id=str(uuid.uuid4()), user_id="jwt-user", phone="", is_active=True)

        # ``async with db_session_async() as db:`` -> mock_session()
        # returns the context manager; __aenter__ yields ``mock_db``.
        mock_db = MagicMock()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = sentinel_user
        mock_db.execute = AsyncMock(return_value=execute_result)

        async_cm = MagicMock()
        async_cm.__aenter__ = AsyncMock(return_value=mock_db)
        async_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "backend.app.auth.session_auth.decode_access_token",
                return_value={"sub": sentinel_user.id},
            ),
            patch("backend.app.auth.session_auth.db_session_async", return_value=async_cm),
        ):
            result = await resolve_multi_user(request)
        assert result is sentinel_user
        assert request.state.auth_source == "session"


class TestMutationSurvivesAuditFailure:
    """Fresh-session pattern: a failing audit commit must not roll back the
    route's session. The mutation succeeds, the response goes through, and
    only the audit row is missing (logged as a warning).

    Complements ``TestAdminAuditFailOpen`` in ``test_admin_router.py``,
    which covers the read path. This one specifically exercises a
    mutation, which historically tripped the worst failure mode (see the
    ``test_delete_user_double_purge_returns_404`` regression that drove
    the move to a fresh audit session).
    """

    def test_deactivate_succeeds_when_audit_commit_fails(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sqlalchemy.orm import Session as SQLASession

        target = _create_user_in_db(is_active=True)
        original_commit = SQLASession.commit

        def commit_with_audit_failure(self: SQLASession) -> None:
            if any(isinstance(obj, AdminAuditLog) for obj in self.new):
                raise RuntimeError("simulated audit commit failure")
            return original_commit(self)

        monkeypatch.setattr(SQLASession, "commit", commit_with_audit_failure)

        # Mutation must succeed — the deactivate took effect, the response
        # body is correct, and the failed audit is contained to the fresh
        # audit session.
        resp = client.post(f"/api/admin/users/{target.id}/deactivate")
        assert resp.status_code == 200
        assert resp.json() == {"id": target.id, "is_active": False}


# ---------------------------------------------------------------------------
# Reported-conversations audit propagation (regression for PR #425).
#
# The original assertions in ``tests/test_admin_reported_conversations.py``
# (``test_writes_audit_row_with_resource_id`` and
# ``test_dismiss_writes_audit_row``) were dropped during the async DB
# conversion: the audit dep stays sync and writes through a separate
# ``SessionLocal()``, which the per-test async connection cannot see
# under READ COMMITTED. The all-sync replacements below restore the
# regression net by mirroring the deactivate_user pattern above:
#   * setup goes through the sync per-test connection (so the audit
#     FK on ``target_user_id`` resolves and the row is persisted),
#   * the route's ``Depends(get_async_db)`` is overridden with a thin
#     proxy that wraps the sync per-test ``Session``, so the route
#     reads its setup back through the same connection.
# This keeps the route + audit context propagation contract under
# regression coverage without re-introducing the cross-connection
# visibility issue that motivated dropping the original tests.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _override_async_db_with_sync(
    db_session: Session,
) -> Generator[Session]:
    """Point ``Depends(get_async_db)`` at the sync per-test connection.

    Mirrors the ``deactivate_user`` pattern in ``TestAuditOnMutation``:
    setup data committed through ``db_session`` is the same data the
    route reads, and the audit dep (sync ``SessionLocal``) sees both.
    Reuses ``_SyncToAsyncSessionProxy`` from ``conftest`` so the proxy
    shim has a single home.
    """
    from tests.multi_user.conftest import MULTI_USER_APP as app
    from tests.multi_user.conftest import _SyncToAsyncSessionProxy

    async def _yield_proxy() -> AsyncGenerator[_SyncToAsyncSessionProxy]:
        yield _SyncToAsyncSessionProxy(db_session)

    app.dependency_overrides[get_async_db] = _yield_proxy
    try:
        yield db_session
    finally:
        app.dependency_overrides.pop(get_async_db, None)


def _seed_report(
    db_session: Session,
    *,
    target_user: User,
    dismissed: bool = False,
) -> ReportedConversation:
    """Insert a ChatSession + ReportedConversation through the sync per-test session."""
    cs = ChatSession(
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        user_id=target_user.id,
        channel="imessage",
    )
    db_session.add(cs)
    db_session.flush()
    report = ReportedConversation(
        user_id=target_user.id,
        session_id=cs.id,
        anchor_seq=None,
        reason="",
        dismissed_at=_dt.datetime.now(_dt.UTC) if dismissed else None,
    )
    db_session.add(report)
    db_session.commit()
    db_session.refresh(report)
    return report


class TestReportedConversationsAudit:
    """Regression guard for the audit-context fields on the reported-
    conversations admin routes.

    The async-driven suite in ``tests/test_admin_reported_conversations.py``
    cannot assert on ``AdminAuditLog`` rows because the audit dep writes
    through a sync ``SessionLocal()`` that does not share the per-test
    async connection. These all-sync tests cover that gap by stamping
    the route's audit-context fields and reading the row back through
    the sync per-test session.
    """

    def test_view_messages_writes_audit_row_with_resource_id(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
        _override_async_db_with_sync: Session,
    ) -> None:
        """``GET /admin/reported-conversations/{id}/messages`` must
        propagate ``target_user_id`` (the user who filed the report),
        ``resource_type='reported_conversation'``, ``resource_id``
        (the report id), and the action string."""
        target = _create_user_in_db()
        report = _seed_report(db_session, target_user=target)

        before = db_session.query(AdminAuditLog).count()
        resp = client.get(f"/api/admin/reported-conversations/{report.id}/messages")
        assert resp.status_code == 200, resp.text

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1
        latest = rows[0]
        assert latest.action == "view_reported_conversation_messages"
        assert latest.target_user_id == target.id
        assert latest.resource_type == "reported_conversation"
        assert latest.resource_id == str(report.id)

    def test_dismiss_writes_audit_row_with_resource_id(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
        _override_async_db_with_sync: Session,
    ) -> None:
        """``POST /admin/reported-conversations/{id}/dismiss`` must
        propagate the same audit-context fields as the read path:
        ``target_user_id`` is the reporting user, ``resource_id`` is
        the report id, and ``action`` matches the dismiss enum value."""
        target = _create_user_in_db()
        report = _seed_report(db_session, target_user=target)

        before = db_session.query(AdminAuditLog).count()
        resp = client.post(f"/api/admin/reported-conversations/{report.id}/dismiss")
        assert resp.status_code == 200, resp.text

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1
        latest = rows[0]
        assert latest.action == "dismiss_reported_conversation"
        assert latest.target_user_id == target.id
        assert latest.resource_type == "reported_conversation"
        assert latest.resource_id == str(report.id)
