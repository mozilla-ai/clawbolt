"""CRUD for named LLM endpoints.

The endpoint's stored credential is the reason most of these exist: it must
never come back out of the API, and re-submitting the form must not blank it.
"""

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import settings
from backend.app.config_store import MASK
from backend.app.database import db_session_async
from backend.app.models import AdminAuditLog, LLMEndpoint, Subscription, User
from backend.app.services.llm_endpoints import reset_llm_endpoint_cache

BASE = "/api/user/model/endpoints"


@pytest.fixture(autouse=True)
def _clear_endpoint_cache() -> Generator[None]:
    reset_llm_endpoint_cache()
    yield
    reset_llm_endpoint_cache()


def _read(name: str) -> LLMEndpoint | None:
    """Read one endpoint row directly.

    This suite runs against the single-user app, which has no sync
    ``db_session`` fixture, and the stored key is the one thing the API
    deliberately will not report, so the assertions that matter have to go to
    the row.
    """

    async def _get() -> LLMEndpoint | None:
        async with db_session_async() as db:
            return (
                await db.execute(select(LLMEndpoint).where(LLMEndpoint.name == name))
            ).scalar_one_or_none()

    return asyncio.run(_get())


def _add_subscription(user_id: str, endpoint: str) -> None:
    async def _add() -> None:
        async with db_session_async() as db:
            db.add(
                Subscription(
                    user_id=user_id,
                    role="user",
                    plan="free",
                    status="active",
                    llm_endpoint_override=endpoint,
                )
            )
            await db.commit()

    asyncio.run(_add())


def _body(**overrides: object) -> dict:
    body: dict = {
        "name": "otari",
        "dialect": "anthropic",
        "base_url": "https://ai.example.test",
        "cache_control": "never",
        "reasoning": "none",
        "pricing": "unpriced",
        "notes": "house gateway",
    }
    body.update(overrides)
    return body


def test_create_returns_the_endpoint_without_the_key(client: TestClient) -> None:
    resp = client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
    assert resp.status_code == 200
    item = resp.json()
    assert item["name"] == "otari"
    assert item["dialect"] == "anthropic"
    assert item["reasoning"] == "none"
    assert item["api_key_set"] is True
    assert "sk-secret" not in resp.text


def test_listing_reports_the_key_as_a_boolean_only(client: TestClient) -> None:
    client.put(f"{BASE}/keyed", json=_body(name="keyed", api_key="sk-secret"))
    client.put(f"{BASE}/bare", json=_body(name="bare"))

    resp = client.get(BASE)
    assert resp.status_code == 200
    by_name = {i["name"]: i for i in resp.json()["items"]}
    assert by_name["keyed"]["api_key_set"] is True
    assert by_name["bare"]["api_key_set"] is False
    assert "sk-secret" not in resp.text


def test_resubmitting_the_mask_keeps_the_stored_key(client: TestClient) -> None:
    """The UI never shows the key, so a form round trip sends the sentinel.

    Treating that as the new value would silently replace a working
    credential with eight asterisks.
    """
    client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
    resp = client.put(f"{BASE}/otari", json=_body(api_key=MASK, notes="edited"))
    assert resp.status_code == 200
    assert resp.json()["api_key_set"] is True

    row = _read("otari")
    assert row is not None
    assert row.api_key == "sk-secret"
    assert row.notes == "edited"


def test_omitting_the_key_also_keeps_it(client: TestClient) -> None:
    client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
    body = _body()
    body.pop("api_key", None)
    assert client.put(f"{BASE}/otari", json=body).status_code == 200
    row = _read("otari")
    assert row is not None and row.api_key == "sk-secret"


def test_an_empty_key_clears_it(client: TestClient) -> None:
    """Distinct from omission: this is how an operator drops a credential."""
    client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
    assert client.put(f"{BASE}/otari", json=_body(api_key="")).status_code == 200
    row = _read("otari")
    assert row is not None and row.api_key == ""


def test_a_name_mismatch_between_path_and_body_is_rejected(client: TestClient) -> None:
    resp = client.put(f"{BASE}/otari", json=_body(name="something-else"))
    assert resp.status_code == 400


def test_an_unknown_dialect_is_rejected(client: TestClient) -> None:
    """The dialect selects the any-llm adapter, so it has to name a real one."""
    resp = client.put(f"{BASE}/otari", json=_body(dialect="not-a-provider"))
    assert resp.status_code == 422
    assert "not-a-provider" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("reasoning", "sideways"), ("cache_control", "sometimes"), ("pricing", "cheap")],
)
def test_capability_columns_reject_values_outside_their_set(
    client: TestClient, field: str, value: str
) -> None:
    resp = client.put(f"{BASE}/otari", json=_body(**{field: value}))
    assert resp.status_code == 422


@pytest.mark.parametrize("name", ["Otari", "has space", "-leading"])
def test_names_are_restricted_to_a_slug(client: TestClient, name: str) -> None:
    """The name goes in a URL and in settings, so it stays a plain slug."""
    resp = client.put(f"{BASE}/{name}", json=_body(name=name))
    assert resp.status_code == 422


def test_an_empty_name_does_not_reach_the_handler(client: TestClient) -> None:
    """``PUT /endpoints/`` is the collection path, which takes no PUT."""
    assert client.put(f"{BASE}/", json=_body(name="")).status_code == 405


class TestEndpointModelListing:
    """Enumerating models through a configured endpoint.

    The provider-scoped route refuses a caller-supplied ``api_base`` because
    honoring one would send a provider key from the server's environment to
    whatever host the caller named. That objection does not reach here: the
    caller names an endpoint, and the base and credential come from the row.
    """

    def test_models_are_listed_through_the_endpoints_own_base_and_key(
        self, client: TestClient
    ) -> None:
        client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
        with patch(
            "backend.app.routers.user_profile.get_models",
            new=AsyncMock(return_value=["gw-model-a", "gw-model-b"]),
        ) as listed:
            resp = client.get(f"{BASE}/otari/models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["models"] == ["gw-model-a", "gw-model-b"]
        assert body["supports_listing"] is True
        # The stored row supplies both, and nothing about them came from the
        # request. That is what makes this safe where a URL parameter is not.
        call = listed.await_args
        assert call is not None
        assert call.args[0] == "anthropic"
        assert call.kwargs["api_base"] == "https://ai.example.test"
        assert call.kwargs["api_key"] == "sk-secret"

    def test_a_dialect_that_cannot_enumerate_is_reported_not_raised(
        self, client: TestClient
    ) -> None:
        """The form has to render this, so a 502 would leave it nothing to say."""
        client.put(f"{BASE}/otari", json=_body())
        with patch(
            "backend.app.routers.user_profile.get_models",
            new=AsyncMock(side_effect=NotImplementedError("no listing here")),
        ):
            resp = client.get(f"{BASE}/otari/models")

        assert resp.status_code == 200
        assert resp.json()["supports_listing"] is False
        assert "no listing" in resp.json()["error"]

    def test_a_failed_call_is_reported_not_raised(self, client: TestClient) -> None:
        client.put(f"{BASE}/otari", json=_body())
        with patch(
            "backend.app.routers.user_profile.get_models",
            new=AsyncMock(side_effect=RuntimeError("gateway said no")),
        ):
            resp = client.get(f"{BASE}/otari/models")

        assert resp.status_code == 200
        assert resp.json()["models"] == []
        assert "gateway said no" in resp.json()["error"]

    def test_listing_an_unknown_endpoint_is_404(self, client: TestClient) -> None:
        assert client.get(f"{BASE}/nope/models").status_code == 404


class TestEndpointProbe:
    """The Test button.

    Shaped like an agent turn rather than a ping, because the failure worth
    catching only appears when tools and reasoning ride the same request.
    """

    def test_the_probe_carries_a_tool_and_the_endpoints_reasoning_shape(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``reasoning: effort`` means the scalar rides along, which is the
        # pairing at least one gateway rejects.
        client.put(f"{BASE}/otari", json=_body(reasoning="effort"))
        monkeypatch.setattr(settings, "reasoning_effort", "high")
        sent = AsyncMock(return_value=object())
        with patch("backend.app.routers.user_profile.amessages", new=sent):
            resp = client.post(f"{BASE}/otari/test", json={"model": "gw-model"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model"] == "gw-model"
        call = sent.await_args
        assert call is not None
        kwargs = call.kwargs
        assert kwargs["tools"], "a probe with no tool cannot catch the tools+reasoning refusal"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["api_base"] == "https://ai.example.test"

    @pytest.mark.parametrize("reasoning", ["auto", "thinking"])
    @pytest.mark.parametrize("effort", ["minimal", "medium", "high", "xhigh"])
    def test_the_probe_leaves_room_for_the_thinking_budget_it_asks_for(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        reasoning: str,
        effort: str,
    ) -> None:
        """A budget must be smaller than ``max_tokens``, or the call is invalid.

        The probe used to send a fixed ``max_tokens=16`` beside whatever
        budget the endpoint's reasoning produced. The smallest budget is
        1024, so every effort level made the request invalid and a healthy
        endpoint came back as failed, for a rule the probe itself broke.

        ``auto`` is in the parameters because it is the default for a newly
        created endpoint, and it resolves to the thinking shape.
        """
        client.put(f"{BASE}/otari", json=_body(reasoning=reasoning))
        monkeypatch.setattr(settings, "reasoning_effort", effort)
        sent = AsyncMock(return_value=object())
        with patch("backend.app.routers.user_profile.amessages", new=sent):
            resp = client.post(f"{BASE}/otari/test", json={"model": "gw-model"})

        assert resp.status_code == 200
        call = sent.await_args
        assert call is not None
        budget = call.kwargs["thinking"]["budget_tokens"]
        assert call.kwargs["max_tokens"] > budget, (
            f"max_tokens={call.kwargs['max_tokens']} must exceed budget={budget}"
        )
        # And the label reaching the UI is words, not a repr of a dict.
        assert "{" not in resp.json()["reasoning"]

    def test_the_probe_is_audited_without_recording_the_providers_words(
        self, client: TestClient
    ) -> None:
        """It spends the operator's credential, so it leaves a trail.

        The gateway's error text is not part of that trail: it goes to the
        caller, and the log is read by more people than can set a credential.
        """

        async def _latest() -> AdminAuditLog | None:
            async with db_session_async() as db:
                return (
                    (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id.desc())))
                    .scalars()
                    .first()
                )

        client.put(f"{BASE}/otari", json=_body())
        with patch(
            "backend.app.routers.user_profile.amessages",
            new=AsyncMock(side_effect=RuntimeError("gateway said no: secret-ish detail")),
        ):
            resp = client.post(f"{BASE}/otari/test", json={"model": "gw-model"})

        assert resp.json()["ok"] is False
        row = asyncio.run(_latest())
        assert row is not None
        assert row.action == "test_llm_endpoint"
        assert row.resource_id == "otari"
        assert row.detail is not None
        assert row.detail["ok"] is False
        assert "secret-ish" not in str(row.detail)

    def test_a_rejection_comes_back_as_a_result_not_an_error(self, client: TestClient) -> None:
        """The gateway's own words are the useful part of a failed test."""
        client.put(f"{BASE}/otari", json=_body())
        with patch(
            "backend.app.routers.user_profile.amessages",
            new=AsyncMock(side_effect=RuntimeError("reasoning_effort not supported with tools")),
        ):
            resp = client.post(f"{BASE}/otari/test", json={"model": "gw-model"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "not supported with tools" in body["detail"]

    def test_an_omitted_model_is_chosen_from_what_the_endpoint_serves(
        self, client: TestClient
    ) -> None:
        client.put(f"{BASE}/otari", json=_body())
        with (
            patch(
                "backend.app.routers.user_profile.get_models",
                new=AsyncMock(return_value=["first-model", "second"]),
            ),
            patch(
                "backend.app.routers.user_profile.amessages", new=AsyncMock(return_value=object())
            ) as sent,
        ):
            resp = client.post(f"{BASE}/otari/test", json={})

        assert resp.status_code == 200
        assert resp.json()["model"] == "first-model"
        call = sent.await_args
        assert call is not None
        assert call.kwargs["model"] == "first-model"

    def test_an_endpoint_that_cannot_be_asked_needs_a_model(self, client: TestClient) -> None:
        client.put(f"{BASE}/otari", json=_body())
        with patch(
            "backend.app.routers.user_profile.get_models",
            new=AsyncMock(side_effect=NotImplementedError()),
        ):
            resp = client.post(f"{BASE}/otari/test", json={})

        assert resp.status_code == 422
        assert "needs a model id" in resp.json()["detail"]

    def test_probing_an_unknown_endpoint_is_404(self, client: TestClient) -> None:
        assert client.post(f"{BASE}/nope/test", json={}).status_code == 404


def test_delete_removes_the_endpoint(client: TestClient) -> None:
    client.put(f"{BASE}/otari", json=_body())
    assert client.delete(f"{BASE}/otari").status_code == 204
    assert _read("otari") is None


def test_deleting_an_unknown_endpoint_is_404(client: TestClient) -> None:
    assert client.delete(f"{BASE}/nope").status_code == 404


def test_delete_is_refused_while_a_setting_selects_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting under a live selection turns every call into a raise.

    The traceback would name the endpoint rather than the deletion, so the
    refusal has to happen here.
    """
    client.put(f"{BASE}/otari", json=_body())
    monkeypatch.setattr(settings, "llm_endpoint", "otari")
    resp = client.delete(f"{BASE}/otari")
    assert resp.status_code == 409
    assert "llm_endpoint" in resp.json()["detail"]


def test_delete_is_refused_while_a_role_selects_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put(f"{BASE}/otari", json=_body())
    monkeypatch.setattr(settings, "heartbeat_endpoint", "otari")
    resp = client.delete(f"{BASE}/otari")
    assert resp.status_code == 409
    assert "heartbeat_endpoint" in resp.json()["detail"]


def test_selecting_an_endpoint_that_does_not_exist_is_rejected(client: TestClient) -> None:
    """The mirror of the delete guard.

    Delete refuses to orphan a live selection. Without this check the
    asymmetry bites: an endpoint cannot be removed out from under a setting,
    but a setting can be pointed at one that was never created, and the
    failure then surfaces on the next message rather than here.
    """
    resp = client.put("/api/user/model/config", json={"llm_endpoint": "never-created"})
    assert resp.status_code == 422
    assert "never-created" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["vision_endpoint", "heartbeat_endpoint", "compaction_endpoint"])
def test_a_secondary_role_endpoint_is_validated_too(client: TestClient, field: str) -> None:
    resp = client.put("/api/user/model/config", json={field: "never-created"})
    assert resp.status_code == 422


def test_selecting_a_configured_endpoint_is_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``update_settings`` mutates the singleton, and nothing else in the
    # suite undoes that. Set the attribute through monkeypatch first so its
    # teardown restores it, or the selection leaks into later tests and
    # blocks deletes with a 409.
    monkeypatch.setattr(settings, "llm_endpoint", "")
    client.put(f"{BASE}/otari", json=_body())
    resp = client.put("/api/user/model/config", json={"llm_endpoint": "otari"})
    assert resp.status_code == 200
    assert resp.json()["llm_endpoint"] == "otari"


def test_writes_are_audited_without_recording_the_key(client: TestClient) -> None:
    """An endpoint names a destination for all traffic and a credential.

    "Who pointed us at that host" has to be answerable, and the log is read
    by more people than can set one, so the key must not be in it.
    """

    async def _rows() -> list[AdminAuditLog]:
        async with db_session_async() as db:
            return list(
                (await db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id.desc())))
                .scalars()
                .all()
            )

    client.put(f"{BASE}/otari", json=_body(api_key="sk-secret"))
    latest = asyncio.run(_rows())[0]
    assert latest.action == "upsert_llm_endpoint"
    assert latest.resource_type == "llm_endpoint"
    assert latest.resource_id == "otari"
    assert latest.detail is not None
    assert latest.detail["api_key_changed"] is True
    assert "sk-secret" not in str(latest.detail)

    assert client.delete(f"{BASE}/otari").status_code == 204
    assert asyncio.run(_rows())[0].action == "delete_llm_endpoint"


def test_delete_is_refused_while_a_user_is_pinned_to_it(
    client: TestClient, test_user: User
) -> None:
    client.put(f"{BASE}/otari", json=_body())
    _add_subscription(test_user.id, "otari")

    resp = client.delete(f"{BASE}/otari")
    assert resp.status_code == 409
    assert "pinned" in resp.json()["detail"]
