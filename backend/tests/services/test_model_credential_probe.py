"""Mocked provider probes use the selected key and ordinary gateway recording."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.audit import service as audit_service
from preloop.services.model_credential_probe import probe_rejected_model_credentials
from tests.services.test_model_credential_health import STORED_KEY, _stored_model


@pytest.fixture(autouse=True)
def enabled_audit_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Own an enabled collector regardless of preceding application shutdowns."""
    monkeypatch.setattr(audit_service, "_audit_service", audit_service.AuditService())


def _marked_model(db: Session, account_id: Any, **overrides: Any) -> models.AIModel:
    """Build a customer model with an existing authentication rejection."""
    from preloop.models.models.secret_reference import UPSTREAM_AUTH_REJECTION_KEY

    model = _stored_model(db, account_id, **overrides)
    now = datetime.now(timezone.utc).isoformat()
    model.credentials_secret.meta_data = {
        UPSTREAM_AUTH_REJECTION_KEY: {
            "status_code": 401,
            "provider": "openrouter",
            "message": "Rejected",
            "first_rejected_at": now,
            "last_rejected_at": now,
        }
    }
    db.flush()
    return model


@pytest.mark.parametrize("status", [200, 401, 402, 403, 429, 500, "timeout"])
def test_openrouter_probe_verdict_and_metering(
    db_session: Session, test_user: models.User, status: int | str
) -> None:
    """Only a fresh authentication rejection refuses; success clears the mark."""
    model = _marked_model(db_session, test_user.account_id)
    db_session.commit()
    request = httpx.Request("GET", "https://openrouter.ai/api/v1/key")
    response = httpx.Response(
        status if isinstance(status, int) else 500,
        request=request,
        json={"error": {"message": f"User not found. {STORED_KEY}"}},
    )
    with (
        patch("preloop.services.model_credential_probe.httpx.Client") as client,
        patch(
            "preloop.services.openai_gateway.OpenAIGatewayService._record_gateway_request"
        ) as record,
        patch(
            "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
        ),
    ):
        get = client.return_value.__enter__.return_value.get
        get.return_value = response
        if status == "timeout":
            get.side_effect = httpx.ReadTimeout("probe timed out", request=request)
        result = probe_rejected_model_credentials(db_session, model=model)
        assert result.auth_rejected is (status == 401)
        assert STORED_KEY not in result.message
        assert STORED_KEY not in str(record.call_args)
        assert "User not found." in result.message if status == 401 else True
        get.assert_called_once_with(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {STORED_KEY}"},
        )
        assert client.call_args.kwargs == {"timeout": 5, "follow_redirects": False}
        record.assert_called_once()
        assert record.call_args.kwargs["endpoint_kind"] == "credential_probe"
        db_session.refresh(model)
        assert model.credentials_rejected is (status != 200)
        if status == 402:
            assert model.credentials_last_error_code == "upstream_billing_rejected"
            assert model.credentials_secret.upstream_auth_rejection is None


def test_other_provider_probe_is_bounded_and_cannot_fallback(
    db_session: Session, test_user: models.User
) -> None:
    """An explicit stored key and hard limits override arbitrary model defaults."""
    model = _marked_model(
        db_session,
        test_user.account_id,
        provider_name="openai",
        model_identifier="gpt-5.4",
        api_endpoint="https://api.openai.com/v1",
    )
    gateway = MagicMock()
    gateway._build_completion_kwargs.return_value = {
        "model": "openai/gpt-5.4",
        "api_key": STORED_KEY,
        "api_base": "https://api.openai.com/v1",
        "max_tokens": 50000,
        "extra_body": {"max_tokens": 50000},
        "fallbacks": ["other-model"],
    }
    gateway._last_upstream_credential_type = "api_key"
    gateway._response_to_dict.return_value = {
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}
    }
    with patch(
        "preloop.services.model_credential_probe.OpenAIGatewayService",
        return_value=gateway,
    ):
        result = probe_rejected_model_credentials(db_session, model=model)
    assert result.auth_rejected is False
    kwargs = gateway.upstream_backend.completion.call_args.kwargs
    assert kwargs["api_key"] == STORED_KEY
    assert kwargs["max_tokens"] == 1
    assert kwargs["timeout"] == 5
    assert kwargs["num_retries"] == 0
    assert "fallbacks" not in kwargs and "extra_body" not in kwargs
    gateway._record_gateway_request.assert_called_once()


def test_owner_email_is_deferred_and_respects_existing_delivery_helper() -> None:
    """Enqueueing never performs email I/O on the request thread."""
    from preloop.services import model_credential_alerts as alerts

    data: dict[str, Any] = {
        "model_name": "Review model",
        "provider": "openrouter",
        "message": "User not found.",
        "first_rejected_at": "2030-01-01T00:00:00Z",
        "remedy": "replace or rotate the API key for this model",
    }
    with (
        patch.object(alerts, "_EXECUTOR") as executor,
        patch.object(alerts, "get_session_factory"),
        patch.object(alerts, "policy_owners", return_value=[object()]),
        patch.object(alerts, "_send_emails") as send,
    ):
        alerts.enqueue_credential_rejection_notice(account_id="synthetic", data=data)
        send.assert_not_called()
        executor.submit.call_args.args[0]()
        send.assert_called_once()
        assert "Preloop login" in send.call_args.args[2]["text"]


def test_free_probe_writes_gateway_usage_and_audit(
    db_session: Session,
    test_user: models.User,
) -> None:
    """A free provider check still has an ordinary, zero-token ledger entry."""
    model = _marked_model(db_session, test_user.account_id)
    db_session.commit()
    with patch("preloop.services.model_credential_probe.httpx.Client") as client:
        client.return_value.__enter__.return_value.get.return_value = httpx.Response(
            200,
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/key"),
            json={"data": {"limit_remaining": 1}},
        )
        result = probe_rejected_model_credentials(db_session, model=model)
    assert result.auth_rejected is False
    usage = crud.crud_api_usage.get_multi(
        db_session, account_id=str(test_user.account_id)
    )
    assert len(usage) == 1
    assert usage[0].endpoint == "/openai/v1/credential-probe"
    assert usage[0].status_code == 200
    assert usage[0].total_tokens == 0
    audit = crud.crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="model_gateway_request"
    )
    assert len(audit) == 1
    assert STORED_KEY not in str(audit[0].details)


def test_late_probe_rejection_cannot_refuse_a_replaced_key(
    db_session: Session,
    test_user: models.User,
) -> None:
    """A provider verdict for the old secret version cannot reject new material."""
    model = _marked_model(db_session, test_user.account_id)
    db_session.commit()

    from preloop.services.openai_gateway import OpenAIGatewayService

    probe_owner: dict[str, Any] = {}
    build = OpenAIGatewayService._build_completion_kwargs

    def capture(gateway: Any, ai_model: Any, **kwargs: Any) -> dict[str, Any]:
        """Capture the probe's session so the synthetic rotation shares its owner."""
        probe_owner.update(db=gateway.db, model=ai_model)
        return build(gateway, ai_model, **kwargs)

    def rotate_while_checking(*args: Any, **kwargs: Any) -> httpx.Response:
        """Replace the key after the probe selected its original material."""
        crud.crud_ai_model.update(
            probe_owner["db"],
            db_obj=probe_owner["model"],
            obj_in={"api_key": "sk-synthetic-replacement"},
        )
        return httpx.Response(
            401,
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/key"),
            json={"error": {"message": "User not found."}},
        )

    with (
        patch.object(
            OpenAIGatewayService,
            "_build_completion_kwargs",
            autospec=True,
            side_effect=capture,
        ),
        patch("preloop.services.model_credential_probe.httpx.Client") as client,
        patch(
            "preloop.services.openai_gateway.OpenAIGatewayService._record_gateway_request"
        ),
    ):
        client.return_value.__enter__.return_value.get.side_effect = (
            rotate_while_checking
        )
        result = probe_rejected_model_credentials(db_session, model=model)
    assert result.auth_rejected is False
    db_session.refresh(model)
    assert model.credentials_rejected is False


@pytest.mark.parametrize("failure", ["usage", "health", "revalidation", "owner_lookup"])
def test_probe_bookkeeping_failures_allow_admission(
    db_session: Session,
    test_user: models.User,
    failure: str,
) -> None:
    """No ledger, audit, outbox or revalidation exception can refuse a trigger."""
    from contextlib import ExitStack

    model = _marked_model(db_session, test_user.account_id)
    db_session.commit()
    with ExitStack() as stack:
        client = stack.enter_context(
            patch("preloop.services.model_credential_probe.httpx.Client")
        )
        client.return_value.__enter__.return_value.get.return_value = httpx.Response(
            401,
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/key"),
            json={"error": {"message": "User not found."}},
        )
        record = stack.enter_context(
            patch(
                "preloop.services.openai_gateway.OpenAIGatewayService._record_gateway_request"
            )
        )
        targets = {
            "health": "preloop.services.model_credential_probe.record_gateway_credential_outcome",
            "revalidation": "preloop.services.model_credential_probe.crud.crud_ai_model.get",
            "owner_lookup": "preloop.services.model_credential_probe.crud.crud_user.get_by_account",
        }
        if failure == "usage":
            record.side_effect = RuntimeError("Synthetic ledger failure")
        else:
            stack.enter_context(
                patch(
                    targets[failure],
                    side_effect=RuntimeError("Synthetic storage failure"),
                )
            )
        result = probe_rejected_model_credentials(db_session, model=model)
    assert result.auth_rejected is False


def test_probe_never_commits_or_rolls_back_the_trigger_session(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Probe health/usage transactions must not finish a caller's pending work."""
    model = _marked_model(db_session, test_user.account_id)
    db_session.commit()
    with (
        patch.object(db_session, "commit", side_effect=AssertionError("Caller commit")),
        patch.object(
            db_session, "rollback", side_effect=AssertionError("Caller rollback")
        ),
        patch("preloop.services.model_credential_probe.httpx.Client") as client,
    ):
        client.return_value.__enter__.return_value.get.return_value = httpx.Response(
            200,
            request=httpx.Request("GET", "https://openrouter.ai/api/v1/key"),
            json={"data": {}},
        )
        result = probe_rejected_model_credentials(db_session, model=model)
    assert result.auth_rejected is False
    rows = crud.crud_api_usage.get_multi(
        db_session, account_id=str(test_user.account_id)
    )
    assert len(rows) == 1
