"""Collection preserves redaction and gateway identity on the core service path."""

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_audit_log
from preloop.services.audit.service import AuditService
from preloop.services.audit.outbox import sanitize_intent_fields
from preloop.utils.audit import log_model_gateway_request


def test_nested_array_secrets_are_redacted_before_row_or_outbox() -> None:
    service = AuditService()
    args = {"rows": [[{"api_key": "synthetic-nested-secret", "label": "kept"}]]}
    assert "synthetic-nested-secret" not in str(service._sanitize_tool_args(args))
    clean = sanitize_intent_fields({"tool_args": args})
    assert "synthetic-nested-secret" not in str(clean)
    assert clean["tool_args"]["rows"][0][0]["label"] == "kept"


def test_reference_kept_nested_arrays_are_redacted() -> None:
    args = {
        "_preloop_reference": True,
        "arg_keys": ["rows"],
        "kept": {
            "$.rows": [[{"password": "synthetic-nested-secret", "label": "kept"}]]
        },
    }
    clean = AuditService()._sanitize_tool_args(args)
    assert "synthetic-nested-secret" not in str(clean)
    assert clean["kept"]["$.rows"][0][0]["label"] == "kept"


def test_gateway_identity_preserves_retry_metadata_in_one_core_row(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.audit import get_audit_service

    get_audit_service().enable()
    log_model_gateway_request(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        api_usage_id="usage-example",
        endpoint="/v1/messages",
        endpoint_kind="messages",
        status_code=200,
        outcome="success",
        requested_model="example-model",
        model_alias="example",
        provider_name="local",
        gateway_provider="local",
        auth_subject_type="gateway_subject",
        request_fingerprint="fingerprint-example",
        gateway_attempt=3,
        is_retry=True,
        retry_of_api_usage_id="previous-example",
        gateway_subject={
            "id": "subject-example",
            "email": "actor@example.com",
            "token": "synthetic-gateway-secret",
        },
    )
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="model_gateway_request"
    )
    assert len(rows) == 1
    details = rows[0].details
    assert details["request_fingerprint"] == "fingerprint-example"
    assert details["gateway_attempt"] == 3
    assert details["is_retry"] is True
    assert details["retry_of_api_usage_id"] == "previous-example"
    assert details["gateway_subject"] == {
        "id": "subject-example",
        "email": "actor@example.com",
    }
    assert "synthetic-gateway-secret" not in str(details)


def test_outbox_gateway_identity_is_whitelisted_before_persistence() -> None:
    clean = sanitize_intent_fields(
        {
            "gateway_subject": {
                "id": "example-subject",
                "token": "synthetic-gateway-secret",
                "external_subject": "x" * 513,
            }
        }
    )
    assert clean["gateway_subject"] == {"id": "example-subject"}
    assert "synthetic-gateway-secret" not in str(clean)
