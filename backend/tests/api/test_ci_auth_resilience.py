"""Credential checks must survive a briefly exhausted or unreachable pool."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeout

from preloop.api.middleware import ci_auth
from preloop.models.crud.ci_principal import CiTokenInspection

TOKEN = "synthetic-human-token-for-cache-test"


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/protected")
    def protected() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def _get(client: TestClient) -> Any:
    return client.get("/protected", headers={"Authorization": f"Bearer {TOKEN}"})


def _pool_timeout(tokens: Any, action: Any) -> Any:
    raise SQLAlchemyPoolTimeout("QueuePool limit of size 6 overflow 14 reached")


def test_pass_through_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def inspect(tokens: Any, action: Any) -> Any:
        calls.append(tokens)
        return CiTokenInspection(recognized=False), True

    monkeypatch.setattr(ci_auth, "_inspect", inspect)
    client = _client()
    assert _get(client).status_code == 200
    assert _get(client).status_code == 200
    assert len(calls) == 1


def test_recent_pass_through_survives_pool_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ci_auth, "_inspect", lambda t, a: (CiTokenInspection(recognized=False), True)
    )
    client = _client()
    assert _get(client).status_code == 200

    # Past the fresh window: a working database would be asked again.
    monkeypatch.setattr(ci_auth.PASS_THROUGH_CACHE, "ttl", 0.0)
    monkeypatch.setattr(ci_auth, "_inspect", _pool_timeout)
    response = _get(client)
    assert response.status_code == 200
    assert response.json() == {"ok": "yes"}


def test_unknown_token_still_503_when_pool_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_auth, "_inspect", _pool_timeout)
    response = _get(_client())
    assert response.status_code == 503
    assert response.json() == {"detail": "Credential verification unavailable"}


def test_stale_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_auth, "_inspect", lambda t, a: (CiTokenInspection(recognized=False), True)
    )
    client = _client()
    assert _get(client).status_code == 200
    monkeypatch.setattr(ci_auth.PASS_THROUGH_CACHE, "ttl", 0.0)
    monkeypatch.setattr(ci_auth.PASS_THROUGH_CACHE, "stale_ttl", 0.0)
    monkeypatch.setattr(ci_auth, "_inspect", _pool_timeout)
    assert _get(client).status_code == 503


def test_machine_credentials_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    def inspect(tokens: Any, action: Any) -> Any:
        calls.append(tokens)
        return CiTokenInspection(recognized=True), False

    monkeypatch.setattr(ci_auth, "_inspect", inspect)
    client = _client()
    assert _get(client).status_code == 401
    assert _get(client).status_code == 401
    assert len(calls) == 2
    monkeypatch.setattr(ci_auth, "_inspect", _pool_timeout)
    assert _get(client).status_code == 503


def test_cache_never_stores_token_values() -> None:
    cache = ci_auth._PassThroughCache(ttl=60, stale_ttl=600, max_entries=2)
    key = cache.key((TOKEN,), None)
    cache.put(key, CiTokenInspection(recognized=False))
    assert TOKEN not in key
    assert TOKEN not in repr(cache._entries)


def test_cache_is_bounded() -> None:
    cache = ci_auth._PassThroughCache(ttl=60, stale_ttl=600, max_entries=2)
    for index in range(5):
        cache.put(str(index), CiTokenInspection(recognized=False))
    assert list(cache._entries) == ["3", "4"]


def test_credential_check_uses_its_own_small_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models.db import session as session_module

    create = MagicMock()
    monkeypatch.setattr(session_module, "create_engine", create)
    monkeypatch.setattr(session_module, "install_pool_hold_diagnostics", MagicMock())
    monkeypatch.setattr(session_module, "_auth_session_factory", None)
    for name in (
        "DATABASE_AUTH_POOL_SIZE",
        "DATABASE_AUTH_MAX_OVERFLOW",
        "DATABASE_AUTH_POOL_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    factory = session_module.get_auth_session_factory("postgresql://u:p@h/db")
    assert session_module.get_auth_session_factory() is factory
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert (kwargs["pool_size"], kwargs["max_overflow"]) == (2, 3)
    assert kwargs["pool_timeout"] == 3
    assert session_module.get_engine_if_initialized() is not create.return_value
    monkeypatch.setattr(session_module, "_auth_session_factory", None)


def test_pool_exhaustion_message_names_the_pool() -> None:
    import inspect

    from preloop.api import app as app_module

    source = inspect.getsource(app_module)
    assert "Database connections are saturated" not in source
    assert "connection pool is exhausted" in source


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
def test_cached_pass_through_still_checks_route_auth(
    monkeypatch: pytest.MonkeyPatch, stale: bool, restricted: bool
) -> None:
    from fastapi import Depends
    from preloop.api.auth import jwt

    app = FastAPI()
    app.dependency_overrides[jwt.get_db_session] = lambda: MagicMock()

    @app.get("/protected")
    def protected(user: Any = Depends(jwt.get_current_user)) -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    key = MagicMock(requires_machine_authorization=restricted, is_active=False)
    lookup = MagicMock(return_value=key)
    monkeypatch.setattr(jwt.crud_api_key, "get_by_key", lookup)
    inspect = MagicMock(return_value=(CiTokenInspection(recognized=False), True))
    monkeypatch.setattr(ci_auth, "_inspect", inspect)
    client = TestClient(app)
    expected = 403 if restricted else 401
    # Middleware caches the classification, but the real route dependency denies it.
    assert _get(client).status_code == expected
    assert len(ci_auth.PASS_THROUGH_CACHE._entries) == 1
    if stale:
        monkeypatch.setattr(ci_auth.PASS_THROUGH_CACHE, "ttl", 0.0)
        inspect.side_effect = _pool_timeout
    assert _get(client).status_code == expected
    assert lookup.call_count == 2
    assert inspect.call_count == (2 if stale else 1)
