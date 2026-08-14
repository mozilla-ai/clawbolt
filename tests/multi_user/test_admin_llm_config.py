"""Tests for admin LLM config endpoints + per-user override resolver."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from any_llm.exceptions import MissingApiKeyError
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import AdminAuditLog, Subscription, User
from backend.app.services.llm_resolver import user_llm_override_resolver
from tests.multi_user.conftest import open_test_db_session


def _create_user(**overrides: object) -> User:
    """Create a minimal User row directly via the OSS session factory."""
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


# ---------------------------------------------------------------------------
# Global LLM config endpoint
# ---------------------------------------------------------------------------


class TestGetLLMConfig:
    def test_returns_current_settings(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        original_provider = settings.llm_provider
        original_model = settings.llm_model
        settings.llm_provider = "anthropic"
        settings.llm_model = "claude-opus-4-5"
        try:
            resp = client.get("/api/admin/config/llm")
            assert resp.status_code == 200
            data = resp.json()
            assert data["llm_provider"] == "anthropic"
            assert data["llm_model"] == "claude-opus-4-5"
            assert "llm_api_base" in data
        finally:
            settings.llm_provider = original_provider
            settings.llm_model = original_model

    def test_non_admin_blocked(
        self,
        client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        sub = Subscription(
            user_id=test_user.id,
            role="user",
            plan="free",
            status="active",
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get("/api/admin/config/llm")
        assert resp.status_code == 403


class TestUpdateLLMConfig:
    def test_update_provider_and_model(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        original_provider = settings.llm_provider
        original_model = settings.llm_model
        try:
            resp = client.put(
                "/api/admin/config/llm",
                json={"llm_provider": "anthropic", "llm_model": "claude-haiku-4-5"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["llm_provider"] == "anthropic"
            assert data["llm_model"] == "claude-haiku-4-5"
            # Settings singleton actually mutated:
            assert settings.llm_provider == "anthropic"
            assert settings.llm_model == "claude-haiku-4-5"
        finally:
            settings.llm_provider = original_provider
            settings.llm_model = original_model

    def test_partial_update_leaves_other_fields(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        original_provider = settings.llm_provider
        original_model = settings.llm_model
        settings.llm_provider = "anthropic"
        settings.llm_model = "claude-opus-4-5"
        try:
            resp = client.put(
                "/api/admin/config/llm",
                json={"llm_model": "claude-sonnet-4-6"},
            )
            assert resp.status_code == 200
            assert settings.llm_provider == "anthropic"
            assert settings.llm_model == "claude-sonnet-4-6"
        finally:
            settings.llm_provider = original_provider
            settings.llm_model = original_model

    def test_empty_body_returns_400(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        resp = client.put("/api/admin/config/llm", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Per-user override endpoints
# ---------------------------------------------------------------------------


class TestGetUserLLMOverride:
    def test_returns_empty_override_with_global_effective(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        original_provider = settings.llm_provider
        original_model = settings.llm_model
        settings.llm_provider = "anthropic"
        settings.llm_model = "claude-opus-4-5"
        try:
            target = _create_user()
            db_session.add(
                Subscription(user_id=target.id, role="user", plan="free", status="active")
            )
            db_session.commit()

            resp = client.get(f"/api/admin/users/{target.id}/llm-config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["user_id"] == target.id
            assert data["llm_provider_override"] == ""
            assert data["llm_model_override"] == ""
            assert data["effective_llm_provider"] == "anthropic"
            assert data["effective_llm_model"] == "claude-opus-4-5"
        finally:
            settings.llm_provider = original_provider
            settings.llm_model = original_model

    def test_returns_404_when_user_missing(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        resp = client.get(f"/api/admin/users/{uuid.uuid4()}/llm-config")
        assert resp.status_code == 404


class TestUpdateUserLLMOverride:
    def test_set_model_only_keeps_global_provider(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        original_provider = settings.llm_provider
        settings.llm_provider = "anthropic"
        try:
            target = _create_user()
            db_session.add(
                Subscription(user_id=target.id, role="user", plan="free", status="active")
            )
            db_session.commit()

            resp = client.put(
                f"/api/admin/users/{target.id}/llm-config",
                json={"llm_model_override": "claude-haiku-4-5"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["llm_provider_override"] == ""
            assert data["llm_model_override"] == "claude-haiku-4-5"
            # Effective values fall through provider, override model:
            assert data["effective_llm_provider"] == "anthropic"
            assert data["effective_llm_model"] == "claude-haiku-4-5"
        finally:
            settings.llm_provider = original_provider

    def test_clear_override_with_empty_string(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        original_provider = settings.llm_provider
        original_model = settings.llm_model
        settings.llm_provider = "anthropic"
        settings.llm_model = "claude-opus-4-5"
        try:
            target = _create_user()
            db_session.add(
                Subscription(
                    user_id=target.id,
                    role="user",
                    plan="free",
                    status="active",
                    llm_provider_override="openai",
                    llm_model_override="gpt-5",
                )
            )
            db_session.commit()

            resp = client.put(
                f"/api/admin/users/{target.id}/llm-config",
                json={"llm_provider_override": "", "llm_model_override": ""},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["llm_provider_override"] == ""
            assert data["llm_model_override"] == ""
            assert data["effective_llm_provider"] == "anthropic"
            assert data["effective_llm_model"] == "claude-opus-4-5"
        finally:
            settings.llm_provider = original_provider
            settings.llm_model = original_model

    def test_omit_field_leaves_unchanged(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        target = _create_user()
        db_session.add(
            Subscription(
                user_id=target.id,
                role="user",
                plan="free",
                status="active",
                llm_provider_override="openai",
                llm_model_override="gpt-5",
            )
        )
        db_session.commit()

        # Only update model, leave provider untouched.
        resp = client.put(
            f"/api/admin/users/{target.id}/llm-config",
            json={"llm_model_override": "gpt-5-mini"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_provider_override"] == "openai"
        assert data["llm_model_override"] == "gpt-5-mini"

    def test_returns_404_when_user_missing(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        resp = client.put(
            f"/api/admin/users/{uuid.uuid4()}/llm-config",
            json={"llm_model_override": "claude-opus-4-5"},
        )
        assert resp.status_code == 404

    def test_admin_can_set_their_own_llm_override(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """An admin may pick the model their own agent uses.

        This endpoint was swept into the self-action guard added for the plan /
        activate / deactivate / reset-quota endpoints, which exist to stop an
        admin escalating their own account. Model choice carries no privilege,
        and blocking it broke the most common legitimate use: trying a model on
        your own account first.
        """
        resp = client.put(
            f"/api/admin/users/{test_user.id}/llm-config",
            json={"llm_model_override": "claude-opus-4-5"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm_model_override"] == "claude-opus-4-5"
        assert body["effective_llm_model"] == "claude-opus-4-5"

    def test_admin_can_clear_their_own_llm_override(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
    ) -> None:
        """The empty-string clear path also works on the admin's own row, so an
        admin can get back to the global default without a DB edit."""
        set_resp = client.put(
            f"/api/admin/users/{test_user.id}/llm-config",
            json={"llm_provider_override": "anthropic", "llm_model_override": "claude-opus-4-5"},
        )
        assert set_resp.status_code == 200

        clear_resp = client.put(
            f"/api/admin/users/{test_user.id}/llm-config",
            json={"llm_provider_override": "", "llm_model_override": ""},
        )
        assert clear_resp.status_code == 200
        body = clear_resp.json()
        assert body["llm_provider_override"] == ""
        assert body["llm_model_override"] == ""

    def test_self_override_still_writes_audit_row(
        self,
        client: TestClient,
        test_user: User,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """Dropping the self-action guard must not cost the paper trail.

        The explicit ``get_current_admin`` parameter went away with the guard,
        so the audit row now depends solely on ``audit_admin`` resolving it.
        Pin that here: a self-targeted change is still attributable.
        """
        before = db_session.query(AdminAuditLog).count()

        resp = client.put(
            f"/api/admin/users/{test_user.id}/llm-config",
            json={"llm_model_override": "claude-haiku-4-5"},
        )
        assert resp.status_code == 200

        db_session.commit()
        rows = db_session.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).all()
        assert len(rows) == before + 1
        latest = rows[0]
        assert latest.action == "update_user_llm_override"
        assert latest.admin_user_id == test_user.id
        assert latest.target_user_id == test_user.id
        assert latest.resource_type == "user_llm_override"

    def test_non_admin_blocked(
        self,
        client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        """The role check survives the removal of the explicit dependency.

        ``update_user_llm_config`` no longer declares
        ``admin: User = Depends(get_current_admin)``, so the 403 for a
        non-admin rests entirely on ``audit_admin`` resolving it.
        """
        db_session.add(
            Subscription(user_id=test_user.id, role="user", plan="free", status="active")
        )
        db_session.commit()

        resp = client.put(
            f"/api/admin/users/{test_user.id}/llm-config",
            json={"llm_model_override": "claude-opus-4-5"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestPremiumUserLLMResolver:
    async def test_returns_none_when_no_override(
        self,
        db_session: Session,
    ) -> None:
        user = _create_user()
        db_session.add(Subscription(user_id=user.id, role="user", plan="free", status="active"))
        db_session.commit()

        assert await user_llm_override_resolver(user.id) is None

    async def test_returns_tuple_when_either_field_set(
        self,
        db_session: Session,
    ) -> None:
        user = _create_user()
        db_session.add(
            Subscription(
                user_id=user.id,
                role="user",
                plan="free",
                status="active",
                llm_model_override="claude-haiku-4-5",
            )
        )
        db_session.commit()

        result = await user_llm_override_resolver(user.id)
        assert result == ("", "claude-haiku-4-5")

    async def test_returns_none_when_user_missing(self) -> None:
        assert await user_llm_override_resolver(str(uuid.uuid4())) is None

    async def test_resolver_returns_full_tuple(
        self,
        db_session: Session,
    ) -> None:
        user = _create_user()
        db_session.add(
            Subscription(
                user_id=user.id,
                role="user",
                plan="free",
                status="active",
                llm_provider_override="openai",
                llm_model_override="gpt-5",
            )
        )
        db_session.commit()

        result = await user_llm_override_resolver(user.id)
        assert result == ("openai", "gpt-5")


# ---------------------------------------------------------------------------
# Provider / model enumeration endpoints (admin LLM config UI dropdowns)
# ---------------------------------------------------------------------------


class TestListLLMProviders:
    def test_returns_known_providers(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        resp = client.get("/api/admin/config/llm/providers")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        names = [p["name"] for p in data["providers"]]
        # any-llm always knows about the major hosted providers.
        assert "anthropic" in names
        assert "openai" in names
        # Each entry has a boolean ``local`` flag.
        for p in data["providers"]:
            assert isinstance(p["local"], bool)

    def test_non_admin_blocked(
        self,
        client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        sub = Subscription(user_id=test_user.id, role="user", plan="free", status="active")
        db_session.add(sub)
        db_session.commit()
        resp = client.get("/api/admin/config/llm/providers")
        assert resp.status_code == 403


class TestListLLMProviderModels:
    def test_success_returns_sorted_models(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        with patch(
            "backend.app.routers.admin.get_models",
            return_value=[
                "claude-opus-4-5",
                "claude-haiku-4-5",
                "claude-sonnet-4-6",
            ],
        ):
            resp = client.get("/api/admin/config/llm/providers/anthropic/models")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["provider"] == "anthropic"
        assert data["supports_listing"] is True
        assert data["error"] is None
        # Sorted alphabetically so the dropdown order is stable.
        assert data["models"] == [
            "claude-haiku-4-5",
            "claude-opus-4-5",
            "claude-sonnet-4-6",
        ]

    def test_unsupported_provider_returns_structured_error(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        """A provider that cannot enumerate models should return 200 with
        ``supports_listing=False`` so the UI can render a text-input
        fallback rather than seeing a generic 5xx and hiding the field.
        """
        with patch(
            "backend.app.routers.admin.get_models",
            side_effect=NotImplementedError("Provider doesn't support listing models."),
        ):
            resp = client.get("/api/admin/config/llm/providers/sometinyprovider/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supports_listing"] is False
        assert data["models"] == []
        assert "support" in (data["error"] or "").lower()

    def test_missing_api_key_returns_structured_error(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        """A missing API key is the operator's problem, not a 5xx.
        ``supports_listing`` stays True so the UI hints "this provider
        could list, but needs a key configured first"."""
        with patch(
            "backend.app.routers.admin.get_models",
            side_effect=MissingApiKeyError("openai", "OPENAI_API_KEY"),
        ):
            resp = client.get("/api/admin/config/llm/providers/openai/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supports_listing"] is True
        assert data["models"] == []
        assert "OPENAI_API_KEY" in (data["error"] or "") or data["error"]

    def test_unexpected_error_is_swallowed_into_response(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        """Network errors etc. produce a structured error response,
        not a 502, so the UI can render an inline "Retry" button."""
        with patch(
            "backend.app.routers.admin.get_models",
            side_effect=RuntimeError("connection refused"),
        ):
            resp = client.get("/api/admin/config/llm/providers/anthropic/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["supports_listing"] is True
        assert data["models"] == []
        assert "connection refused" in (data["error"] or "")

    def test_non_admin_blocked(
        self,
        client: TestClient,
        test_user: User,
        db_session: Session,
    ) -> None:
        sub = Subscription(user_id=test_user.id, role="user", plan="free", status="active")
        db_session.add(sub)
        db_session.commit()
        resp = client.get("/api/admin/config/llm/providers/anthropic/models")
        assert resp.status_code == 403


class TestListLLMProviderModelsApiBase:
    """The dropdown must enumerate the endpoint the agent actually calls.

    ``core.py`` passes ``api_base=settings.llm_api_base`` to ``amessages`` on
    every request. This endpoint used to call any-llm with no ``api_base`` at
    all, so a deployment routed through a gateway listed the wrong catalog and,
    because the gateway's virtual key was presented to the real provider,
    usually just failed: "Failed to list models: 401 invalid x-api-key".
    """

    def test_uses_the_configured_api_base(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        original = settings.llm_api_base
        settings.llm_api_base = "https://gateway.example.com"
        try:
            with patch(
                "backend.app.routers.admin.get_models",
                return_value=["alias-a:model-1"],
            ) as mock_get_models:
                resp = client.get("/api/admin/config/llm/providers/anthropic/models")
            assert resp.status_code == 200, resp.text
            assert resp.json()["models"] == ["alias-a:model-1"]
            await_args = mock_get_models.await_args
            assert await_args is not None, "get_models was never called"
            assert await_args.kwargs["api_base"] == "https://gateway.example.com"
        finally:
            settings.llm_api_base = original

    def test_unset_api_base_setting_passes_none(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        """A deployment talking straight to a provider is unchanged."""
        original = settings.llm_api_base
        settings.llm_api_base = None
        try:
            with patch(
                "backend.app.routers.admin.get_models",
                return_value=[],
            ) as mock_get_models:
                resp = client.get("/api/admin/config/llm/providers/anthropic/models")
            assert resp.status_code == 200, resp.text
            await_args = mock_get_models.await_args
            assert await_args is not None, "get_models was never called"
            assert await_args.kwargs["api_base"] is None
        finally:
            settings.llm_api_base = original

    def test_caller_cannot_choose_the_endpoint(
        self,
        client: TestClient,
        test_subscription: Subscription,
    ) -> None:
        """No caller-supplied ``api_base``, by design.

        Accepting one would let an admin make the server deliver a provider API
        key from its own environment to a host of their choosing. FastAPI ignores
        undeclared query params, so an attempt is silently inert rather than an
        error; assert the configured base is still what gets used.
        """
        original = settings.llm_api_base
        settings.llm_api_base = "https://gateway.example.com"
        try:
            with patch(
                "backend.app.routers.admin.get_models",
                return_value=[],
            ) as mock_get_models:
                resp = client.get(
                    "/api/admin/config/llm/providers/anthropic/models",
                    params={"api_base": "http://attacker.example.com"},
                )
            assert resp.status_code == 200, resp.text
            await_args = mock_get_models.await_args
            assert await_args is not None, "get_models was never called"
            assert await_args.kwargs["api_base"] == "https://gateway.example.com"
        finally:
            settings.llm_api_base = original

    def test_effective_api_base_lands_on_the_audit_row(
        self,
        client: TestClient,
        test_subscription: Subscription,
        db_session: Session,
    ) -> None:
        """A later "why was this listing empty?" has to be answerable from the
        audit trail, not only from the (rotating) application log."""
        original = settings.llm_api_base
        settings.llm_api_base = "https://gateway.example.com"
        try:
            with patch(
                "backend.app.routers.admin.get_models",
                return_value=[],
            ):
                resp = client.get("/api/admin/config/llm/providers/anthropic/models")
            assert resp.status_code == 200, resp.text
        finally:
            settings.llm_api_base = original

        db_session.commit()
        latest = (
            db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "view_llm_provider_models")
            .order_by(AdminAuditLog.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.detail is not None
        assert latest.detail.get("api_base") == "https://gateway.example.com"
