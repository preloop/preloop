"""A provider's rejection of a stored API key is kept on the model.

Before this, an API key that the provider no longer recognised (revoked, its
provider account deleted, or pasted from the wrong account) failed every run
with an upstream 401 while the model row still read ``active``. Nothing told
the owner which stored key was dead or since when.

These tests pin the whole loop: the gateway records the rejection on the
credential secret, the model reads as ``error`` with a summary, an accepted
request or a key rotation clears it, and platform-owned models and OAuth
credentials are left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.secret_reference import (
    UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL,
    crud_secret_reference,
)
from preloop.models.models.secret_reference import UPSTREAM_AUTH_REJECTION_KEY
from preloop.schemas.ai_model import AIModelRead
from preloop.services.gateway_execution import GatewayModelSnapshot
from preloop.services.model_credential_health import (
    UPSTREAM_AUTH_REJECTED_CODE,
    record_gateway_credential_outcome,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import get_secret_service
from tests.credential_fixtures import ai_model_with_secret

STORED_KEY = "sk-or-v1-synthetic-key-that-is-never-sent-anywhere"

_real_record = type(crud_secret_reference).record_upstream_auth_rejection


def _stored_model(db: Session, account_id, **overrides) -> models.AIModel:
    fields = {
        "id": uuid4(),
        "account_id": account_id,
        "name": "Review model",
        "provider_name": "openrouter",
        "model_identifier": "deepseek/deepseek-chat",
        "api_endpoint": "https://openrouter.ai/api/v1",
        "api_key": STORED_KEY,
        **overrides,
    }
    model = ai_model_with_secret(**fields)
    db.add(model)
    db.flush()
    return model


def _reject(db: Session, model: models.AIModel, *, now: datetime) -> bool:
    return crud_secret_reference.record_upstream_auth_rejection(
        db,
        ai_model_id=model.id,
        provider="openrouter",
        status_code=401,
        message="User not found.",
        now=now,
        commit=False,
    )


def test_rejected_api_key_reads_as_a_failing_credential(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id)
    assert model.credentials_status == "active"
    assert model.credentials_last_error is None

    rejected_at = datetime(2026, 11, 12, 13, 54, 36, tzinfo=timezone.utc)
    assert _reject(db_session, model, now=rejected_at) is True
    db_session.refresh(model)

    assert model.credentials_rejected is True
    assert model.credentials_status == "error"
    assert model.credentials_last_error_code == UPSTREAM_AUTH_REJECTED_CODE
    assert model.credentials_last_failed_at == rejected_at
    summary = model.credentials_last_error
    assert summary is not None
    assert "openrouter rejected the stored API key" in summary
    assert "HTTP 401: User not found." in summary
    assert rejected_at.isoformat() in summary
    # The stored status is what OAuth lineage queries filter on; it must not
    # change for an API key, and no key material may reach the summary.
    assert model.credentials_secret.status == "active"
    assert STORED_KEY not in summary

    read = AIModelRead.model_validate(model)
    assert read.credentials_status == "error"
    assert read.credentials_last_error == summary
    assert read.credentials_last_error_code == UPSTREAM_AUTH_REJECTED_CODE
    assert read.credentials_last_failed_at == rejected_at


def test_retry_burst_writes_once_and_keeps_the_first_rejection_time(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id)
    first = datetime(2026, 11, 3, 14, 56, 18, tzinfo=timezone.utc)

    assert _reject(db_session, model, now=first) is True
    assert _reject(db_session, model, now=first + timedelta(seconds=1)) is False

    later = first + UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL + timedelta(days=9)
    assert _reject(db_session, model, now=later) is True
    db_session.refresh(model)

    rejection = model.credentials_upstream_rejection
    assert rejection is not None
    assert rejection["first_rejected_at"] == first.isoformat()
    assert rejection["last_rejected_at"] == later.isoformat()
    assert f"Rejected since {first.isoformat()}" in model.credentials_last_error


def test_accepted_request_clears_the_rejection(db_session: Session, test_user) -> None:
    model = _stored_model(db_session, test_user.account_id)
    _reject(db_session, model, now=datetime(2026, 11, 12, 13, 54, tzinfo=timezone.utc))
    accepted_at = datetime(2026, 11, 12, 15, 17, 8, tzinfo=timezone.utc)

    assert (
        crud_secret_reference.clear_upstream_auth_rejection(
            db_session, ai_model_id=model.id, now=accepted_at, commit=False
        )
        is True
    )
    db_session.refresh(model)

    assert model.credentials_rejected is False
    assert model.credentials_status == "active"
    assert model.credentials_last_error is None
    assert model.credentials_last_error_code is None
    assert model.credentials_last_failed_at is None
    assert UPSTREAM_AUTH_REJECTION_KEY not in model.credentials_secret.meta_data
    assert model.credentials_last_verified_at is not None
    # Nothing left to clear: a healthy key costs no write.
    assert (
        crud_secret_reference.clear_upstream_auth_rejection(
            db_session, ai_model_id=model.id, now=accepted_at, commit=False
        )
        is False
    )


def test_rotating_the_key_clears_the_rejection(db_session: Session, test_user) -> None:
    model = _stored_model(db_session, test_user.account_id)
    _reject(db_session, model, now=datetime(2026, 11, 12, 13, 54, tzinfo=timezone.utc))
    db_session.refresh(model)
    assert model.credentials_status == "error"

    get_secret_service().rotate_secret_reference(
        db_session,
        secret_id=model.credentials_secret_id,
        account_id=test_user.account_id,
        secret_value="sk-or-v1-synthetic-replacement-key",
    )
    db_session.refresh(model)

    assert model.credentials_status == "active"
    assert model.credentials_rejected is False
    assert model.credentials_version == 2


def test_model_without_a_stored_key_is_left_alone(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id, api_key=None)

    assert _reject(db_session, model, now=datetime.now(timezone.utc)) is False
    assert model.credentials_status is None


@pytest.mark.parametrize(
    ("status_code", "error_class", "credential_type", "account_owned"),
    [
        # Subscription logins have their own refresh-failure tracking.
        (401, "upstream_auth", "oauth", True),
        # No credential was sent upstream, so the 401 is not about a key.
        (401, "upstream_auth", None, True),
        # A platform-owned key is the operator's problem, not the customer's.
        (401, "upstream_auth", "api_key", False),
        # 403 is a permission or region answer, not a dead key.
        (403, "upstream_auth", "api_key", True),
        (402, "upstream_quota_exhausted", "api_key", True),
        (502, "upstream_error", "api_key", True),
    ],
)
def test_only_an_upstream_401_on_a_customer_api_key_is_recorded(
    status_code: int,
    error_class: str,
    credential_type: str | None,
    account_owned: bool,
) -> None:
    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4() if account_owned else None,
        provider_name="openrouter",
        credentials_rejected=False,
    )
    with patch(
        "preloop.services.model_credential_health.crud_secret_reference"
    ) as crud:
        record_gateway_credential_outcome(
            MagicMock(),
            ai_model=model,
            status_code=status_code,
            error_class=error_class,
            error_detail="User not found.",
            credential_type=credential_type,
        )
    crud.record_upstream_auth_rejection.assert_not_called()
    crud.clear_upstream_auth_rejection.assert_not_called()


def test_long_provider_message_is_trimmed_before_it_is_stored() -> None:
    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        provider_name="openrouter",
        credentials_rejected=False,
    )
    with patch(
        "preloop.services.model_credential_health.crud_secret_reference"
    ) as crud:
        record_gateway_credential_outcome(
            MagicMock(),
            ai_model=model,
            status_code=401,
            error_class="upstream_auth",
            error_detail="User   not\nfound. " + "x" * 500,
            credential_type="api_key",
        )
    kwargs = crud.record_upstream_auth_rejection.call_args.kwargs
    assert kwargs["message"].startswith("User not found. x")
    # 200 kept characters plus the sanitizer's note of how many were dropped.
    assert kwargs["message"][:200].endswith("x")
    assert 200 < len(kwargs["message"]) < 260
    assert kwargs["provider"] == "openrouter"
    assert kwargs["status_code"] == 401


def test_provider_message_is_made_safe_for_jsonb(
    db_session: Session, test_user
) -> None:
    """A NUL or lone surrogate in the provider body must not lose the record."""
    model = _stored_model(db_session, test_user.account_id)
    snapshot = GatewayModelSnapshot.from_model(model)

    with patch(
        "preloop.models.crud.secret_reference.CRUDSecretReference"
        ".record_upstream_auth_rejection",
        autospec=True,
        side_effect=lambda self, db, **kw: _real_record(self, db, commit=False, **kw),
    ):
        record_gateway_credential_outcome(
            db_session,
            ai_model=snapshot,
            status_code=401,
            error_class="upstream_auth",
            error_detail="User\x00 not found.\ud800",
            credential_type="api_key",
        )
    db_session.flush()
    db_session.refresh(model)

    assert model.credentials_status == "error"
    assert model.credentials_upstream_rejection["message"] == "User not found."


def test_account_row_marked_hosted_is_not_tracked() -> None:
    """Billing treats ``meta_data.hosted`` as operator-paid; so must this."""
    model = ai_model_with_secret(
        id=uuid4(),
        account_id=uuid4(),
        name="Hosted model",
        provider_name="deepseek",
        model_identifier="deepseek-flash",
        meta_data={"hosted": True},
        api_key=STORED_KEY,
    )

    secret_crud = _record_through_gateway(
        GatewayModelSnapshot.from_model(model),
        status_code=401,
        error_class="upstream_auth",
        credential_type="api_key",
    )

    secret_crud.record_upstream_auth_rejection.assert_not_called()


def test_late_401_from_the_old_key_does_not_mark_a_rotated_key(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id)
    in_flight = GatewayModelSnapshot.from_model(model)
    assert in_flight.credentials_secret_id == model.credentials_secret_id
    assert in_flight.credentials_version == 1

    get_secret_service().rotate_secret_reference(
        db_session,
        secret_id=model.credentials_secret_id,
        account_id=test_user.account_id,
        secret_value="sk-or-v1-synthetic-replacement-key",
    )

    written = crud_secret_reference.record_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        secret_id=in_flight.credentials_secret_id,
        version=in_flight.credentials_version,
        provider="openrouter",
        status_code=401,
        message="User not found.",
        now=datetime.now(timezone.utc),
        commit=False,
    )
    db_session.refresh(model)

    assert written is False
    assert model.credentials_status == "active"


def test_late_success_from_the_old_key_does_not_clear_a_new_keys_rejection(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id)
    in_flight = GatewayModelSnapshot.from_model(model)
    get_secret_service().rotate_secret_reference(
        db_session,
        secret_id=model.credentials_secret_id,
        account_id=test_user.account_id,
        secret_value="sk-or-v1-synthetic-replacement-key",
    )
    assert _reject(db_session, model, now=datetime.now(timezone.utc)) is True

    cleared = crud_secret_reference.clear_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        secret_id=in_flight.credentials_secret_id,
        version=in_flight.credentials_version,
        now=datetime.now(timezone.utc),
        commit=False,
    )
    db_session.refresh(model)

    assert cleared is False
    assert model.credentials_status == "error"


def test_verdict_for_a_replaced_secret_row_is_ignored(
    db_session: Session, test_user
) -> None:
    model = _stored_model(db_session, test_user.account_id)

    written = crud_secret_reference.record_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        secret_id=uuid4(),
        version=1,
        provider="openrouter",
        status_code=401,
        message="User not found.",
        now=datetime.now(timezone.utc),
        commit=False,
    )

    assert written is False
    assert model.credentials_status == "active"


def _record_through_gateway(
    model: GatewayModelSnapshot | models.AIModel,
    *,
    status_code: int,
    error_class: str | None,
    credential_type: str | None,
    owns_db_session: bool = True,
) -> MagicMock:
    """Run the gateway's usage recording for one request and return the CRUD."""
    account_id = model.account_id or uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    api_key = SimpleNamespace(
        id=uuid4(),
        user_id=user.id,
        account_id=account_id,
        name="flow-execution-key",
        context_data={},
    )
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(token="token", user=user, api_key=api_key),
    )
    service._owns_db_session = owns_db_session
    service._begin_request_accounting()
    service._last_upstream_credential_type = credential_type
    usage_row = SimpleNamespace(
        id=uuid4(),
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=status_code,
        timestamp=datetime.now(timezone.utc),
        cost_source="provider",
        estimated_cost=0.0,
        auth_subject_type="api_key",
        runtime_session_id=None,
        runtime_principal_type=None,
        runtime_principal_id=None,
        runtime_principal_name=None,
        flow_id=None,
        flow_execution_id=None,
        upstream_request_id=None,
        meta_data={},
    )
    with (
        patch.object(service, "_resolve_runtime_session", return_value=None),
        patch.object(service, "_resolve_managed_agent_id", return_value=None),
        patch.object(service, "_pricing_override_for_request", return_value=None),
        patch("preloop.services.openai_gateway.crud_api_usage") as usage_crud,
        patch("preloop.services.openai_gateway.crud_runtime_session"),
        patch("preloop.services.openai_gateway.crud_runtime_session_activity"),
        patch("preloop.services.openai_gateway.log_model_gateway_request"),
        patch("preloop.services.openai_gateway.ModelGatewayEventEmitter"),
        patch("preloop.services.gateway_usage_search.GatewayUsageSearchService"),
        patch("preloop.services.session_search_index.index_gateway_interaction"),
        patch(
            "preloop.services.openai_gateway.estimate_ai_model_usage_cost_detailed",
            return_value=SimpleNamespace(
                cost=0.0, source="provider", pricing_snapshot=None
            ),
        ),
        patch(
            "preloop.services.model_credential_health.crud_secret_reference"
        ) as secret_crud,
    ):
        usage_crud.log_gateway_request.return_value = usage_row
        usage_crud.get_gateway_attempt_summary.return_value = {}
        service._record_gateway_request_inner(
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=status_code,
            duration=0.1,
            ai_model=model,
            requested_model="review/deepseek",
            response_payload=None,
            upstream_response=None,
            endpoint_kind="responses",
            error_detail="User not found." if status_code >= 400 else None,
            error_class=error_class,
        )
    return secret_crud


def _snapshot(*, rejected: bool) -> GatewayModelSnapshot:
    account_id = uuid4()
    model = ai_model_with_secret(
        id=uuid4(),
        account_id=account_id,
        name="Review model",
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-chat",
        api_key=STORED_KEY,
    )
    if rejected:
        model.credentials_secret.meta_data = {
            UPSTREAM_AUTH_REJECTION_KEY: {
                "provider": "openrouter",
                "status_code": 401,
                "message": "User not found.",
                "first_rejected_at": "2026-11-03T14:56:18+00:00",
                "last_rejected_at": "2026-11-12T13:54:36+00:00",
            }
        }
    snapshot = GatewayModelSnapshot.from_model(model)
    assert snapshot.credentials_rejected is rejected
    return snapshot


def test_gateway_records_an_upstream_401_against_the_models_key() -> None:
    snapshot = _snapshot(rejected=False)

    secret_crud = _record_through_gateway(
        snapshot,
        status_code=401,
        error_class="upstream_auth",
        credential_type="api_key",
    )

    secret_crud.record_upstream_auth_rejection.assert_called_once()
    kwargs = secret_crud.record_upstream_auth_rejection.call_args.kwargs
    assert kwargs["ai_model_id"] == snapshot.id
    assert kwargs["secret_id"] == snapshot.credentials_secret_id
    assert kwargs["version"] == snapshot.credentials_version
    assert kwargs["provider"] == "openrouter"
    assert kwargs["status_code"] == 401
    assert kwargs["message"] == "User not found."
    secret_crud.clear_upstream_auth_rejection.assert_not_called()


def test_gateway_clears_the_rejection_when_the_provider_accepts_the_key() -> None:
    snapshot = _snapshot(rejected=True)

    secret_crud = _record_through_gateway(
        snapshot, status_code=200, error_class=None, credential_type="api_key"
    )

    secret_crud.clear_upstream_auth_rejection.assert_called_once()
    kwargs = secret_crud.clear_upstream_auth_rejection.call_args.kwargs
    assert kwargs["ai_model_id"] == snapshot.id
    assert kwargs["secret_id"] == snapshot.credentials_secret_id
    assert kwargs["version"] == snapshot.credentials_version
    secret_crud.record_upstream_auth_rejection.assert_not_called()


def test_gateway_success_on_a_healthy_key_touches_nothing() -> None:
    secret_crud = _record_through_gateway(
        _snapshot(rejected=False),
        status_code=200,
        error_class=None,
        credential_type="api_key",
    )

    secret_crud.record_upstream_auth_rejection.assert_not_called()
    secret_crud.clear_upstream_auth_rejection.assert_not_called()


def test_internal_callers_keep_their_transaction_untouched() -> None:
    secret_crud = _record_through_gateway(
        _snapshot(rejected=False),
        status_code=401,
        error_class="upstream_auth",
        credential_type="api_key",
        owns_db_session=False,
    )

    secret_crud.record_upstream_auth_rejection.assert_not_called()
