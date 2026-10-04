"""Publication issuance cannot widen an execution's trusted startup authority."""

from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import jwt
import anyio
import pytest
from fastapi import HTTPException, Response

from preloop.api.endpoints import publication_credentials as endpoint
from preloop.config import settings
from preloop.services.trusted_publisher import PublicationError


async def _refresh(*args: Any) -> dict[str, str]:
    """Exercise the same worker/loop bridge FastAPI uses for this sync route."""
    return await anyio.to_thread.run_sync(
        lambda: endpoint.refresh_publication_credential(*args)
    )


def claims() -> dict[str, Any]:
    return {
        "account_id": uuid4(),
        "execution_id": uuid4(),
        "tracker_id": uuid4(),
        "repository_url": "https://github.com/example/project.git",
    }


@pytest.mark.parametrize("authorization", ["", "Bearer invalid"])
def test_normal_credentials_rejected(authorization: str) -> None:
    with pytest.raises(HTTPException) as error:
        endpoint.publication_claims(authorization)
    assert error.value.status_code == 401


def test_capability_exceeds_installation_token_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now(UTC)

    class Clock:
        @staticmethod
        def now(tz: tzinfo | None) -> datetime:
            return start

    monkeypatch.setattr(endpoint, "datetime", Clock)
    context = claims()
    token = endpoint.mint_publication_capability(
        **{k: str(v) for k, v in context.items()}
    )

    class Later:
        @staticmethod
        def now(tz: tzinfo | None) -> datetime:
            return start + timedelta(hours=2)

    monkeypatch.setattr(jwt.api_jwt, "datetime", Later)
    assert endpoint.publication_claims("Bearer " + token) == {
        **context,
        "aud": endpoint.AUDIENCE,
        "exp": int((start + timedelta(hours=24, minutes=5)).timestamp()),
    }
    with pytest.raises(HTTPException):
        endpoint.publication_claims("Bearer " + token[:-8] + "tampered")


@pytest.mark.parametrize(
    "bad",
    [
        {"aud": "flow-artifact"},
        {"exp": 1},
        {"account_id": []},
        {"repository_url": []},
        {"aud": [endpoint.AUDIENCE]},
    ],
)
def test_bad_claims_rejected(bad: dict[str, Any]) -> None:
    context = {k: str(v) for k, v in claims().items()}
    token = jwt.encode(
        {
            **context,
            "aud": endpoint.AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=1),
            **bad,
        },
        settings.security.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as error:
        endpoint.publication_claims("Bearer " + token)
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["PENDING", "INITIALIZING", "COMPLETED", "FAILED", "CANCELLED", "PAUSED"]
)
async def test_terminal_or_not_running_does_not_mint(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    context = claims()
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(return_value=SimpleNamespace(status=status)),
    )
    mint = AsyncMock()
    monkeypatch.setattr(endpoint, "mint_repository_lease", mint)
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 409
    mint.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["execution", "tracker"])
async def test_tenancy_missing_does_not_mint(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    context = claims()
    execution_get = Mock(
        return_value=None
        if missing == "execution"
        else SimpleNamespace(status="RUNNING")
    )
    tracker_get = Mock(return_value=None)
    monkeypatch.setattr(endpoint.crud_flow_execution, "get", execution_get)
    monkeypatch.setattr(endpoint.crud_tracker, "get", tracker_get)
    mint = AsyncMock()
    monkeypatch.setattr(endpoint, "mint_repository_lease", mint)
    db = Mock()
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, db)
    assert error.value.status_code == 404
    execution_get.assert_called_once_with(
        db,
        id=context["execution_id"],
        account_id=str(context["account_id"]),
        refresh=True,
    )
    if missing == "tracker":
        tracker_get.assert_called_once_with(
            db, id=context["tracker_id"], account_id=str(context["account_id"])
        )
    mint.assert_not_called()


@pytest.mark.asyncio
async def test_other_execution_rejected_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = claims()
    get = Mock()
    monkeypatch.setattr(endpoint.crud_flow_execution, "get", get)
    with pytest.raises(HTTPException) as error:
        await _refresh(uuid4(), Response(), context, Mock())
    assert error.value.status_code == 403
    get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_exact_bound_repository_and_sanitized_issuance(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    context = claims()
    tracker = Mock()
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(return_value=SimpleNamespace(status="RUNNING")),
    )
    monkeypatch.setattr(endpoint.crud_tracker, "get", Mock(return_value=tracker))
    mint = AsyncMock(
        return_value=SimpleNamespace(
            token="fresh-secret", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    )
    if failure:
        mint.side_effect = PublicationError("sensitive-provider-response")
    monkeypatch.setattr(endpoint, "mint_repository_lease", mint)
    response = Response()
    if failure:
        with pytest.raises(HTTPException) as error:
            await _refresh(context["execution_id"], response, context, Mock())
        assert error.value.detail == "publication_credential_unavailable"
    else:
        result = await _refresh(context["execution_id"], response, context, Mock())
        assert result["token"] == "fresh-secret"
        assert response.headers["cache-control"] == "no-store"
    assert mint.call_args.args == (tracker, context["repository_url"])
    assert mint.call_args.kwargs["write"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mid_mint", [False, True])
async def test_stop_intent_blocks_new_authority(
    monkeypatch: pytest.MonkeyPatch, mid_mint: bool
) -> None:
    context = claims()
    running = SimpleNamespace(status="RUNNING", stop_requested_at=None)
    stopped = SimpleNamespace(status="RUNNING", stop_requested_at=datetime.now(UTC))
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(side_effect=[running, stopped] if mid_mint else [stopped]),
    )
    monkeypatch.setattr(endpoint.crud_tracker, "get", Mock(return_value=Mock()))
    mint = AsyncMock(
        return_value=SimpleNamespace(
            token="fresh-token", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    )
    monkeypatch.setattr(endpoint, "mint_repository_lease", mint)
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 409
    assert mint.call_count == int(mid_mint)


@pytest.mark.asyncio
async def test_crud_runs_off_loop_and_async_provider_runs_on_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    context = claims()
    loop_thread = threading.get_ident()
    query_threads: list[int] = []

    def execution_get(*args: Any, **kwargs: Any) -> SimpleNamespace:
        query_threads.append(threading.get_ident())
        return SimpleNamespace(status="RUNNING")

    lazy_load_threads: list[int] = []

    class LazyTracker:
        def __init__(self) -> None:
            self._installation: SimpleNamespace | None = None

        @property
        def oauth_installation(self) -> SimpleNamespace:
            if self._installation is None:
                assert threading.get_ident() != loop_thread
                lazy_load_threads.append(threading.get_ident())
                self._installation = SimpleNamespace(external_id="123")
            return self._installation

    def tracker_get(*args: Any, **kwargs: Any) -> LazyTracker:
        query_threads.append(threading.get_ident())
        return LazyTracker()

    async def issue(*args: Any, **kwargs: Any) -> SimpleNamespace:
        assert threading.get_ident() == loop_thread
        assert args[0].oauth_installation.external_id == "123"
        return SimpleNamespace(
            token="fresh-token", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )

    monkeypatch.setattr(endpoint.crud_flow_execution, "get", execution_get)
    monkeypatch.setattr(endpoint.crud_tracker, "get", tracker_get)
    monkeypatch.setattr(endpoint, "mint_repository_lease", issue)
    await _refresh(context["execution_id"], Response(), context, Mock())
    assert len(query_threads) == 3
    assert all(thread != loop_thread for thread in query_threads)
    assert len(lazy_load_threads) == 1
    assert lazy_load_threads[0] != loop_thread


# ---------------------------------------------------------------------------
# Managed Bitbucket Cloud dispatch (issue #1065): the same execution-bound
# capability reacquires a fresh access token from the provider resolver.
# ---------------------------------------------------------------------------


def _managed_tracker(workspace: str = "ws", repository: str | None = "repo"):
    return SimpleNamespace(
        tracker_type="bitbucket",
        auth_type="managed_oauth",
        connection_details={"workspace": workspace, "repository": repository},
    )


def _bitbucket_claims() -> dict[str, Any]:
    context = claims()
    context["repository_url"] = "https://bitbucket.org/ws/repo.git"
    return context


class _Resolver:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _fresh(token: str, seconds: int = 3600) -> SimpleNamespace:
    return SimpleNamespace(
        access_token=token,
        expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        rotation_version=2,
        git_username="x-token-auth",
    )


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(return_value=SimpleNamespace(status="RUNNING", stop_requested_at=None)),
    )
    mint = AsyncMock()
    monkeypatch.setattr(endpoint, "mint_repository_lease", mint)
    return mint


@pytest.fixture
def bitbucket_resolver(monkeypatch: pytest.MonkeyPatch):
    from preloop.services import managed_credentials as mc

    resolver = _Resolver([])
    mc.register_managed_resolver("bitbucket", resolver)
    yield resolver
    mc.register_managed_resolver("bitbucket", None)


@pytest.mark.asyncio
async def test_managed_bitbucket_reacquires_fresh_token_bound_to_destination(
    monkeypatch: pytest.MonkeyPatch, running: AsyncMock, bitbucket_resolver: _Resolver
) -> None:
    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    bitbucket_resolver.outcomes = [_fresh("fresh-bitbucket-token-b")]
    response = Response()
    result = await _refresh(context["execution_id"], response, context, Mock())
    assert result["token"] == "fresh-bitbucket-token-b"
    assert result["username"] == "x-token-auth"
    assert response.headers["cache-control"] == "no-store"
    running.assert_not_called()  # no GitHub App path for a managed grant
    call = bitbucket_resolver.calls[0]
    assert call["account_id"] == context["account_id"]
    assert call["tracker_id"] == context["tracker_id"]
    assert call["provider"] == "bitbucket"
    assert call["repository"] == "repo"
    assert call["force_refresh"] is False


@pytest.mark.asyncio
async def test_managed_bitbucket_near_expiry_forces_one_rotation(
    monkeypatch: pytest.MonkeyPatch, running: AsyncMock, bitbucket_resolver: _Resolver
) -> None:
    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    bitbucket_resolver.outcomes = [_fresh("almost-dead", 5), _fresh("rotated-b")]
    result = await _refresh(context["execution_id"], Response(), context, Mock())
    assert result["token"] == "rotated-b"
    assert [c["force_refresh"] for c in bitbucket_resolver.calls] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_url",
    [
        "https://bitbucket.org/other-ws/repo.git",
        "https://bitbucket.org/ws/other-repo.git",
        "https://github.com/ws/repo.git",
        "http://bitbucket.org/ws/repo.git",
        "https://user:pw@bitbucket.org/ws/repo.git",
        "https://bitbucket.org/ws/repo/extra.git",
    ],
)
async def test_managed_bitbucket_wrong_destination_denied_before_resolver(
    monkeypatch: pytest.MonkeyPatch,
    running: AsyncMock,
    bitbucket_resolver: _Resolver,
    repository_url: str,
) -> None:
    context = _bitbucket_claims()
    context["repository_url"] = repository_url
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 403
    assert error.value.detail == "publication_destination_mismatch"
    assert bitbucket_resolver.calls == []


@pytest.mark.asyncio
async def test_managed_bitbucket_workspace_only_binding_accepts_any_repository(
    monkeypatch: pytest.MonkeyPatch, running: AsyncMock, bitbucket_resolver: _Resolver
) -> None:
    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_tracker,
        "get",
        Mock(return_value=_managed_tracker(repository=None)),
    )
    bitbucket_resolver.outcomes = [_fresh("token-b")]
    result = await _refresh(context["execution_id"], Response(), context, Mock())
    assert result["token"] == "token-b"
    assert bitbucket_resolver.calls[0]["repository"] == "repo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "detail"),
    [
        ("reconnect", 409, "publication_reconnect_required"),
        ("permission", 403, "publication_destination_forbidden"),
        ("unavailable", 502, "publication_credential_unavailable"),
        ("missing", 502, "publication_credential_unavailable"),
    ],
)
async def test_managed_bitbucket_failures_are_typed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    running: AsyncMock,
    bitbucket_resolver: _Resolver,
    failure: str,
    status: int,
    detail: str,
) -> None:
    from preloop.services import managed_credentials as mc

    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    outcomes = {
        "reconnect": mc.ManagedReconnectRequiredError("invalid_grant"),
        "permission": mc.ManagedCredentialPermissionError("forbidden"),
        "unavailable": RuntimeError("provider body: secret-refresh-token"),
    }
    if failure == "missing":
        mc.register_managed_resolver("bitbucket", None)
    else:
        bitbucket_resolver.outcomes = [outcomes[failure]]
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == status
    assert error.value.detail == detail
    assert "secret-refresh-token" not in str(error.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "PENDING"])
async def test_managed_bitbucket_terminal_execution_cannot_reacquire(
    monkeypatch: pytest.MonkeyPatch, bitbucket_resolver: _Resolver, status: str
) -> None:
    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(return_value=SimpleNamespace(status=status, stop_requested_at=None)),
    )
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 409
    assert bitbucket_resolver.calls == []


@pytest.mark.asyncio
async def test_managed_bitbucket_stop_during_resolution_withholds_token(
    monkeypatch: pytest.MonkeyPatch, bitbucket_resolver: _Resolver
) -> None:
    context = _bitbucket_claims()
    running_state = SimpleNamespace(status="RUNNING", stop_requested_at=None)
    stopped = SimpleNamespace(status="RUNNING", stop_requested_at=datetime.now(UTC))
    monkeypatch.setattr(
        endpoint.crud_flow_execution, "get", Mock(side_effect=[running_state, stopped])
    )
    monkeypatch.setattr(
        endpoint.crud_tracker, "get", Mock(return_value=_managed_tracker())
    )
    bitbucket_resolver.outcomes = [_fresh("token-b")]
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 409
    assert len(bitbucket_resolver.calls) == 1


@pytest.mark.asyncio
async def test_managed_bitbucket_wrong_tenant_tracker_is_missing(
    monkeypatch: pytest.MonkeyPatch, running: AsyncMock, bitbucket_resolver: _Resolver
) -> None:
    context = _bitbucket_claims()
    tracker_get = Mock(return_value=None)
    monkeypatch.setattr(endpoint.crud_tracker, "get", tracker_get)
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 404
    assert tracker_get.call_args.kwargs["account_id"] == str(context["account_id"])
    assert bitbucket_resolver.calls == []


@pytest.mark.asyncio
async def test_pasted_bitbucket_token_is_not_dispatched_to_resolver(
    monkeypatch: pytest.MonkeyPatch, bitbucket_resolver: _Resolver
) -> None:
    """A pasted token keeps its legacy semantics: no managed reacquisition."""
    context = _bitbucket_claims()
    monkeypatch.setattr(
        endpoint.crud_flow_execution,
        "get",
        Mock(return_value=SimpleNamespace(status="RUNNING", stop_requested_at=None)),
    )
    tracker = SimpleNamespace(
        tracker_type="bitbucket",
        auth_type="oauth_token",
        connection_details={"workspace": "ws"},
        oauth_installation=None,
    )
    monkeypatch.setattr(endpoint.crud_tracker, "get", Mock(return_value=tracker))
    bitbucket_resolver.outcomes = [_fresh("must-not-be-used")]
    with pytest.raises(HTTPException) as error:
        await _refresh(context["execution_id"], Response(), context, Mock())
    assert error.value.status_code == 502
    assert bitbucket_resolver.calls == []
