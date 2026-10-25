"""Delegated identities are token-free before audit queueing and searchable."""

from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.services.audit import service as audit
from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log


@pytest.mark.parametrize("denied", [False, True])
def test_grant_audit_persists_identity_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    test_user: models.User,
    denied: bool,
) -> None:
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)

    def schedule(
        factory: Callable[[], Any], callback: Callable[..., Any], **kwargs: Any
    ) -> None:
        kwargs.pop("priority", None)
        kwargs.pop("allow_without_loop", None)
        queued.append(kwargs)
        callback(factory(), **kwargs)

    monkeypatch.setattr(audit, "schedule_audit_write", schedule)
    grant = {
        "available": True,
        "active": not denied,
        "scope": ["records:read"],
        "sub": "subject-example",
        "client_id": "delegated-client",
        "consent_ref": "consent-example",
        "exp": 2000000000,
        "cached": True,
        "token": "synthetic-secret",
        "client_secret": "synthetic-secret",
    }
    service = audit.AuditService()
    if denied:
        service.log_policy_decision_async(
            db_factory=lambda: db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            tool_name="read_record",
            action="deny",
            rule_description="grant_inactive",
            extra_details={"grant": grant},
        )
    else:
        service.log_tool_call_async(
            db_factory=lambda: db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            tool_name="read_record",
            tool_args={},
            result="success",
            grant=grant,
        )
    assert "synthetic-secret" not in str(queued)
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, consent_ref="consent-example"
    )
    assert len(rows) == 1
    assert rows[0].action == ("policy_deny" if denied else "tool_call")
    expected = {k: v for k, v in grant.items() if k not in {"token", "client_secret"}}
    expected["cache_hit"] = True
    assert rows[0].details["grant"] == expected
    assert grant["token"] == "synthetic-secret"  # caller input is not mutated


@pytest.mark.parametrize("grouped", [False, True])
def test_audit_api_consent_filter_is_account_scoped(
    audit_client: TestClient,
    db_session: Session,
    test_user: models.User,
    grouped: bool,
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic other account"}
    )
    for account_id, reference in [
        (test_user.account_id, "consent-example"),
        (test_user.account_id, "consent-example-other"),
        (other.id, "consent-example"),
    ]:
        crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            action="policy_deny",
            status="denied",
            resource_type="tool",
            resource_id="read_record",
            details={"grant": {"consent_ref": reference}},
        )
    path = "/api/v1/audit-logs" + ("/grouped" if grouped else "")
    response = audit_client.get(path, params={"consent_ref": "consent-example"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    entries = data["groups"] if grouped else data["logs"]
    row = entries[0]["primary_event"] if grouped else entries[0]
    assert row["details"]["grant"]["consent_ref"] == "consent-example"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_replayed_approval_grant_is_sanitized_before_queue_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    test_user: models.User,
    asynchronous: bool,
) -> None:
    from uuid import uuid4

    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)

    def schedule(
        factory: Callable[[], Any], callback: Callable[..., Any], **kwargs: Any
    ) -> None:
        queued.append(kwargs)
        callback(factory(), **kwargs)

    monkeypatch.setattr(audit, "schedule_audit_write", schedule)
    grant = {
        "active": True,
        "scope": ["read"],
        "consent_ref": "replayed-consent",
        "cached": True,
        "token": "synthetic-bearer",
        "client_secret": "synthetic-secret",
    }
    details = {"grant": grant}
    service = audit.AuditService()
    kwargs = dict(
        account_id=test_user.account_id,
        approval_id=uuid4(),
        tool_name="read_record",
        status="executed",
        extra_details=details,
    )
    if asynchronous:
        service.log_approval_tool_executed_async(
            db_factory=lambda: db_session, **kwargs
        )
    else:
        service.log_approval_tool_executed(db=db_session, **kwargs)
    assert "synthetic-secret" not in str(queued)
    assert "synthetic-bearer" not in str(queued)
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, consent_ref="replayed-consent"
    )
    assert len(rows) == 1 and rows[0].action == "approval_tool_executed"
    assert rows[0].details["grant"] == {
        "active": True,
        "scope": ["read"],
        "consent_ref": "replayed-consent",
        "cached": True,
        "cache_hit": True,
        "sub": None,
        "client_id": None,
    }
    assert details["grant"]["token"] == "synthetic-bearer"


@pytest.mark.parametrize("kind", ["tool", "policy", "approval"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "grant", ["synthetic-secret", [{"token": "synthetic-secret"}], 42]
)
def test_invalid_grant_shape_preserves_audit_without_queueing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    test_user: models.User,
    kind: str,
    asynchronous: bool,
    grant: Any,
) -> None:
    """Malformed optional metadata cannot drop evidence or enter the queue."""
    from uuid import uuid4

    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kwargs: None)

    def schedule(
        factory: Callable[[], Any], callback: Callable[..., Any], **kwargs: Any
    ) -> None:
        kwargs.pop("priority", None)
        kwargs.pop("allow_without_loop", None)
        queued.append(kwargs)
        callback(factory(), **kwargs)

    monkeypatch.setattr(audit, "schedule_audit_write", schedule)
    service = audit.AuditService()
    kwargs: dict[str, Any] = {
        "account_id": test_user.account_id,
        "tool_name": "malformed-grant",
    }
    if kind == "tool":
        kwargs.update(tool_args={}, result="success", grant=grant)
        method = service.log_tool_call_async if asynchronous else service.log_tool_call
    elif kind == "policy":
        kwargs.update(action="deny", extra_details={"grant": grant})
        method = (
            service.log_policy_decision_async
            if asynchronous
            else service.log_policy_decision
        )
    else:
        kwargs.update(
            approval_id=uuid4(), status="executed", extra_details={"grant": grant}
        )
        method = (
            service.log_approval_tool_executed_async
            if asynchronous
            else service.log_approval_tool_executed
        )
    if asynchronous:
        method(db_factory=lambda: db_session, **kwargs)
    else:
        method(db=db_session, **kwargs)
    assert "synthetic-secret" not in str(queued)
    rows = crud_audit_log.get_by_account(db_session, account_id=test_user.account_id)
    assert len(rows) == 1
    assert rows[0].details.get("grant") is None
    assert "synthetic-secret" not in str(rows[0].details)
