"""Tests for the subscription-OAuth credential export endpoint.

Offboarding must be able to restore an agent's local login: subscription
refresh tokens are single-use, so after any server-side refresh the
Preloop-held bundle is the only live lineage. The export endpoint returns
that bundle to the authenticated operator — and must refuse to export
anything that is not a principal-bound subscription OAuth credential.
"""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from preloop.api.endpoints import ai_models
from preloop.models.models.user import User
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
    CredentialRefreshError,
    ResolvedModelCredentials,
)

from tests.conftest import maybe_await


@pytest.fixture
def mock_user() -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    return user


def _mock_model(account_id: uuid.UUID) -> MagicMock:
    model = MagicMock()
    model.id = uuid.uuid4()
    model.account_id = account_id
    return model


def _patch_model_lookup(mocker: MockerFixture, model) -> MagicMock:
    crud = mocker.patch(
        "preloop.api.endpoints.ai_models.crud_ai_model", new_callable=MagicMock
    )
    crud.get.return_value = model
    return crud


def _patch_secret_service(mocker: MockerFixture) -> MagicMock:
    service = MagicMock()
    mocker.patch(
        "preloop.api.endpoints.ai_models.get_secret_service", return_value=service
    )
    return service


def _resolved(credential_type: str, payload: dict) -> ResolvedModelCredentials:
    return ResolvedModelCredentials(
        credential_type=credential_type,
        backend_type="local_encrypted",
        value=payload.get("access"),
        payload=payload,
    )


@pytest.mark.asyncio
async def test_export_returns_live_claude_bundle(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _resolved(
        ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
        {
            "type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            "access": "live-access",
            "refresh": "live-refresh",
            "expires": 1789000000000,
        },
    )

    response = await maybe_await(
        ai_models.export_ai_model_credentials(
            model_id=model.id, db=MagicMock(), current_user=mock_user
        )
    )

    assert response.credential_type == ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
    assert response.access == "live-access"
    assert response.refresh == "live-refresh"
    assert response.expires == 1789000000000
    assert response.account_id is None
    # The bundle must be refreshed if stale, so the exported copy is live.
    _, kwargs = service.resolve_ai_model_credentials.call_args
    assert kwargs["allow_refresh"] is True


@pytest.mark.asyncio
async def test_export_returns_codex_account_id(mock_user: User, mocker: MockerFixture):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _resolved(
        OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        {
            "type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "access": "codex-access",
            "refresh": "codex-refresh",
            "expires": 1789000000000,
            "account_id": "chatgpt-account",
        },
    )

    response = await maybe_await(
        ai_models.export_ai_model_credentials(
            model_id=model.id, db=MagicMock(), current_user=mock_user
        )
    )

    assert response.credential_type == OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE
    assert response.account_id == "chatgpt-account"


@pytest.mark.asyncio
async def test_export_refuses_api_key_credentials(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _resolved(
        "api_key", {"type": "api_key", "api_key": "sk-plain"}
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_export_refuses_models_without_credentials(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_export_hides_other_account_models(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(uuid.uuid4())  # different account
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 404
    service.resolve_ai_model_credentials.assert_not_called()


@pytest.mark.asyncio
async def test_export_maps_refresh_failure_to_502(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.side_effect = CredentialRefreshError(
        "refresh failed", provider="anthropic_claude_code", status_code=403
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_export_rejects_bundle_without_access_token(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _resolved(
        ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
        {"type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE, "refresh": "r"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 409


def _codex_resolved(expires: object = 1789000000000) -> ResolvedModelCredentials:
    return _resolved(
        OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        {
            "type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            "access": "codex-access-secret",
            "refresh": "codex-refresh-secret",
            "expires": expires,
            "account_id": "chatgpt-account",
        },
    )


@pytest.mark.asyncio
async def test_export_returns_last_refresh_and_writes_audit_line(
    mock_user: User, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
):
    model = _mock_model(mock_user.account_id)
    model.credentials_secret.last_verified_at = datetime(2026, 9, 27, 8, 35, 17)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _codex_resolved()

    with caplog.at_level(logging.INFO, logger="preloop.api.endpoints.ai_models"):
        response = await maybe_await(
            ai_models.export_ai_model_credentials(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )

    assert response.last_refresh == datetime(2026, 9, 27, 8, 35, 17, tzinfo=UTC)
    audit = [
        record.getMessage()
        for record in caplog.records
        if "Exported subscription OAuth credential" in record.getMessage()
    ]
    assert len(audit) == 1
    assert str(model.id) in audit[0]
    assert OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE in audit[0]
    assert "codex-access-secret" not in audit[0]
    assert "codex-refresh-secret" not in audit[0]


@pytest.mark.asyncio
async def test_marker_returns_rotation_fields_without_tokens(
    mock_user: User, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
):
    model = _mock_model(mock_user.account_id)
    model.credentials_secret.last_verified_at = datetime(
        2026, 9, 27, 8, 35, 17, tzinfo=UTC
    )
    model.credentials_secret.status = "active"
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _codex_resolved()

    with caplog.at_level(logging.INFO, logger="preloop.api.endpoints.ai_models"):
        response = await maybe_await(
            ai_models.read_ai_model_credential_marker(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )

    body = response.model_dump(mode="json")
    assert body == {
        "credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        "expires": 1789000000000,
        "last_refresh": "2026-09-27T08:35:17Z",
        "credentials_status": "active",
        "account_id": "chatgpt-account",
    }
    assert "codex-access-secret" not in str(body)
    assert "codex-refresh-secret" not in str(body)
    # The marker read must never rotate the grant by itself.
    _, kwargs = service.resolve_ai_model_credentials.call_args
    assert kwargs["allow_refresh"] is False
    # Only exports are audit-logged; the cheap marker read is not an export.
    assert not any(
        "Exported subscription OAuth credential" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_marker_tolerates_missing_expires_and_timestamp(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    model.credentials_secret = None
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _codex_resolved(expires=True)

    response = await maybe_await(
        ai_models.read_ai_model_credential_marker(
            model_id=model.id, db=MagicMock(), current_user=mock_user
        )
    )

    assert response.expires is None
    assert response.last_refresh is None
    assert response.credentials_status is None


@pytest.mark.asyncio
async def test_marker_refuses_api_key_credentials(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(mock_user.account_id)
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)
    service.resolve_ai_model_credentials.return_value = _resolved(
        "api_key", {"type": "api_key", "api_key": "sk-plain"}
    )

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.read_ai_model_credential_marker(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_marker_hides_other_account_models(
    mock_user: User, mocker: MockerFixture
):
    model = _mock_model(uuid.uuid4())
    _patch_model_lookup(mocker, model)
    service = _patch_secret_service(mocker)

    with pytest.raises(HTTPException) as exc_info:
        await maybe_await(
            ai_models.read_ai_model_credential_marker(
                model_id=model.id, db=MagicMock(), current_user=mock_user
            )
        )
    assert exc_info.value.status_code == 404
    service.resolve_ai_model_credentials.assert_not_called()
