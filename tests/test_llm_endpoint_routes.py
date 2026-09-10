"""CRUD for named LLM endpoints.

The endpoint's stored credential is the reason most of these exist: it must
never come back out of the API, and re-submitting the form must not blank it.
"""

import asyncio
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.config import settings
from backend.app.config_store import MASK
from backend.app.database import db_session_async
from backend.app.models import LLMEndpoint, Subscription, User
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


def test_delete_is_refused_while_a_user_is_pinned_to_it(
    client: TestClient, test_user: User
) -> None:
    client.put(f"{BASE}/otari", json=_body())
    _add_subscription(test_user.id, "otari")

    resp = client.delete(f"{BASE}/otari")
    assert resp.status_code == 409
    assert "pinned" in resp.json()["detail"]
