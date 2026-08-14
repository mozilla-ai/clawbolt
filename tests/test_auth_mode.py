"""AUTH_MODE dispatch: single_user default, multi_user resolver delegation.

The default path carries the risk here. ``single_user`` is what every
self-hosted deployment runs, and none of them set AUTH_MODE at all, so a
regression in the default is invisible to anyone who only tests the new
mode. Half of these tests exist to pin the default in place.
"""

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.auth.dependencies import (
    LOCAL_USER_ID,
    get_current_user,
    get_current_user_resolver,
    set_current_user_resolver,
    validate_auth_mode,
)
from backend.app.config import settings
from backend.app.models import User


def _request() -> Request:
    """A minimal ASGI request; enough for a resolver to read headers."""
    return Request({"type": "http", "method": "GET", "path": "/api/me", "headers": []})


@pytest.fixture(autouse=True)
def _clear_resolver() -> Generator[None]:
    """Leave the module-level resolver unset, whatever the test did."""
    set_current_user_resolver(None)
    yield
    set_current_user_resolver(None)


def test_default_mode_is_single_user() -> None:
    """No AUTH_MODE in the environment means today's behavior.

    Self-hosters upgrade without touching their .env, so this default is
    the whole compatibility contract.
    """
    assert settings.auth_mode == "single_user"


@pytest.mark.asyncio()
async def test_single_user_resolves_without_credentials(
    async_db: async_sessionmaker,
) -> None:
    """The default path authenticates a request carrying no credentials."""
    async with async_db() as db:
        user = await get_current_user(_request(), db)
        assert user.user_id == LOCAL_USER_ID


@pytest.mark.asyncio()
async def test_single_user_ignores_a_registered_resolver(
    async_db: async_sessionmaker,
) -> None:
    """Mode decides, not resolver presence.

    Premium registers its resolver at import time. If that registration
    alone flipped behavior, importing the plugin would silently turn on
    multi-user auth in a deployment that never asked for it.
    """

    async def _never(request: Request) -> User:
        raise AssertionError("resolver must not run in single_user mode")

    set_current_user_resolver(_never)
    async with async_db() as db:
        user = await get_current_user(_request(), db)
        assert user.user_id == LOCAL_USER_ID


@pytest.mark.asyncio()
async def test_multi_user_delegates_to_the_resolver(
    async_db: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """multi_user hands the request to the resolver and returns its user."""
    seen: list[Request] = []
    expected = User(user_id="resolved@example.com")

    async def _resolver(request: Request) -> User:
        seen.append(request)
        return expected

    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    set_current_user_resolver(_resolver)
    request = _request()
    async with async_db() as db:
        user = await get_current_user(request, db)

    assert user is expected
    assert seen == [request], "the resolver must receive the live request"


@pytest.mark.asyncio()
async def test_multi_user_propagates_resolver_rejection(
    async_db: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 from the resolver reaches the caller unchanged."""

    async def _reject(request: Request) -> User:
        raise HTTPException(status_code=401, detail="Invalid token")

    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    set_current_user_resolver(_reject)
    async with async_db() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(_request(), db)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid token"


@pytest.mark.asyncio()
async def test_multi_user_without_resolver_refuses_rather_than_falling_back(
    async_db: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authentication-bypass regression.

    Falling back to the single-user path here would hand the first row in
    ``users`` to an unauthenticated caller, which is worse than the 500:
    a multi-tenant deployment would serve one tenant's data to anyone.
    """
    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    async with async_db() as db:
        db.add(User(user_id="tenant@example.com"))
        await db.commit()

    async with async_db() as db:
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(_request(), db)
    assert exc_info.value.status_code == 500


def test_validate_auth_mode_rejects_multi_user_without_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup fails loudly instead of 500ing every request."""
    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    with pytest.raises(RuntimeError, match="no current-user resolver"):
        validate_auth_mode()


def test_validate_auth_mode_accepts_multi_user_with_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolver(request: Request) -> User:
        raise AssertionError("not called during validation")

    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    set_current_user_resolver(_resolver)
    validate_auth_mode()


def test_validate_auth_mode_imports_the_plugin_before_deciding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not rely on someone else having imported the plugin.

    Nothing earlier in the lifespan imports PREMIUM_PLUGIN:
    ``get_settings_store()`` reaches it only on the SETTINGS_STORE=db path,
    via ``get_kek_provider()``. Without the forced import here, a file-backed
    deployment fails to boot with a perfectly good plugin installed, and the
    error message tells the operator to install the plugin they already have.
    """
    module_name = "fake_auth_mode_plugin"
    (tmp_path / f"{module_name}.py").write_text(
        "from backend.app.auth.dependencies import set_current_user_resolver\n"
        "from backend.app.models import User\n"
        "\n"
        "\n"
        "async def _resolver(request):\n"
        "    return User(user_id='tenant@example.com')\n"
        "\n"
        "\n"
        "set_current_user_resolver(_resolver)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(settings, "premium_plugin", module_name)
    monkeypatch.setattr(settings, "settings_store", "file")
    monkeypatch.setattr(settings, "auth_mode", "multi_user")

    try:
        validate_auth_mode()
        assert get_current_user_resolver() is not None
    finally:
        sys.modules.pop(module_name, None)


def test_validate_auth_mode_accepts_the_default() -> None:
    """single_user needs no resolver, which is the point of the default."""
    validate_auth_mode()


def test_multi_user_resolves_through_a_real_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher still works as a FastAPI dependency, not just as a call.

    Every other test here invokes ``get_current_user`` directly, which
    proves the branching but not the wiring: ``request: Request`` has to
    be injectable for the multi_user path to reach a resolver at all. A
    throwaway app is enough, and keeps this off the real router, where
    the ``client`` fixture overrides the dependency it would test.
    """

    async def _resolver(request: Request) -> User:
        assert request.headers.get("x-token") == "secret"
        return User(user_id="resolved@example.com")

    monkeypatch.setattr(settings, "auth_mode", "multi_user")
    set_current_user_resolver(_resolver)

    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"user_id": user.user_id}

    with TestClient(app) as client:
        response = client.get("/whoami", headers={"x-token": "secret"})

    assert response.status_code == 200
    assert response.json() == {"user_id": "resolved@example.com"}


def test_set_current_user_resolver_round_trips() -> None:
    async def _resolver(request: Request) -> User:
        raise AssertionError("not called")

    assert get_current_user_resolver() is None
    set_current_user_resolver(_resolver)
    assert get_current_user_resolver() is _resolver
    set_current_user_resolver(None)
    assert get_current_user_resolver() is None
