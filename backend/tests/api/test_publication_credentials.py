"""Publication issuance cannot widen an execution's trusted startup authority."""

from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException, Response

from preloop.api.endpoints import publication_credentials as endpoint
from preloop.config import settings
from preloop.services.trusted_publisher import PublicationError


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
        await endpoint.refresh_publication_credential(
            context["execution_id"], Response(), context, Mock()
        )
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
        await endpoint.refresh_publication_credential(
            context["execution_id"], Response(), context, db
        )
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
        await endpoint.refresh_publication_credential(
            uuid4(), Response(), context, Mock()
        )
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
            await endpoint.refresh_publication_credential(
                context["execution_id"], response, context, Mock()
            )
        assert error.value.detail == "publication_credential_unavailable"
    else:
        result = await endpoint.refresh_publication_credential(
            context["execution_id"], response, context, Mock()
        )
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
        await endpoint.refresh_publication_credential(
            context["execution_id"], Response(), context, Mock()
        )
    assert error.value.status_code == 409
    assert mint.call_count == int(mid_mint)
