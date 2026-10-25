"""Regression coverage for permission refusals and persisted audit privacy."""

import asyncio
import functools
import hashlib
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.schemas.session_search import SessionSearchRequest
from preloop.services import session_search_audit
from preloop.services.audit import outbox
from preloop.services.audit.service import AuditService
from preloop.utils import permissions


@pytest.mark.parametrize("async_handler", [False, True])
@pytest.mark.parametrize("gate", ["rbac", "restricted_ci", "handler"])
def test_permission_refusal_is_recorded_once_at_outer_gate(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    async_handler: bool,
    gate: str,
) -> None:
    """RBAC/CI refusals persist once; an audited handler is not recorded twice."""

    def plugin(permission: str) -> Any:
        def decorate(func: Any) -> Any:
            @functools.wraps(func)
            async def call(*args: Any, **kwargs: Any) -> Any:
                if gate == "rbac":
                    raise HTTPException(403, "RBAC denied")
                result = func(*args, **kwargs)
                return await result if asyncio.iscoroutine(result) else result

            return call

        return decorate

    monkeypatch.setattr(permissions, "_plugin_require_permission", plugin)
    actor: Any = test_user
    if gate == "restricted_ci":
        actor = CiAuthorizationContext(
            account_id=test_user.account_id,
            principal_id=uuid4(),
            key_id=uuid4(),
            project_id=uuid4(),
            flow_id=uuid4(),
            tracker_id=uuid4(),
            repository_identifier="example",
            repository_slug="example/repo",
            tracker_type="github",
            tracker_url=None,
            actions=frozenset(),
        )

    def body(*, db: Session, current_user: Any, payload: SessionSearchRequest) -> None:
        assert gate == "handler", "refused request entered the endpoint"
        session_search_audit.record_denied_endpoint_search(
            permission="view_runtime_sessions",
            kwargs={"db": db, "current_user": current_user, "payload": payload},
        )
        raise HTTPException(403, "Search scope denied")

    if async_handler:

        async def search_runtime_sessions(
            *, db: Session, current_user: Any, payload: SessionSearchRequest
        ) -> None:
            body(db=db, current_user=current_user, payload=payload)
    else:

        def search_runtime_sessions(
            *, db: Session, current_user: Any, payload: SessionSearchRequest
        ) -> None:
            body(db=db, current_user=current_user, payload=payload)

    handler = permissions.require_permission("view_runtime_sessions")(
        search_runtime_sessions
    )
    with pytest.raises(HTTPException) as raised:
        result = handler(
            db=db_session,
            current_user=actor,
            payload=SessionSearchRequest(query="private refused query"),
        )
        if async_handler:
            asyncio.run(result)
    assert raised.value.status_code == 403
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="query"
    )
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert "private refused query" not in str(rows[0].details)
    if gate == "restricted_ci":
        assert rows[0].user_id is None
        assert rows[0].details["actor_type"] == "restricted_ci"
        assert rows[0].details["actor_api_key_id"] == str(actor.key_id)


@pytest.mark.parametrize("opt_in", [None, False, "true", True])
def test_legacy_service_search_uses_core_hash_and_explicit_opt_in(
    db_session: Session,
    test_user: models.User,
    opt_in: Any,
) -> None:
    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = {session_search_audit.QUERY_TEXT_OPT_IN_KEY: opt_in}
    db_session.flush()
    service = AuditService()
    service.enable()
    service.log_session_search(
        db_session,
        account_id=test_user.account_id,
        surface="console",
        status="success",
        query="private query",
    )
    row = crud_audit_log.get_by_account(db_session, account_id=test_user.account_id)[0]
    assert row.details["query_hash"] == session_search_audit.query_hash("private query")
    assert row.details["query_recorded"] is (opt_in is True)
    assert ("query" in row.details) is (opt_in is True)


def test_query_text_setting_read_failure_is_not_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.audit.service import _record_query_text

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic account read failure")

    monkeypatch.setattr(crud_account, "get", fail)
    assert _record_query_text(None, uuid4()) is False


def test_approval_result_and_errors_are_safe_in_row_and_intent(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AuditService()
    service.enable()
    fields = dict(
        account_id=test_user.account_id,
        approval_id=uuid4(),
        tool_name="example",
        status="failed",
        result_preview="private result",
        error="token=synthetic-credential",
        extra_details={
            "result_preview": "private overridden result",
            "error": "password=synthetic-password",
        },
    )
    service.log_approval_tool_executed(db_session, **fields)
    row = crud_audit_log.get_by_account(db_session, account_id=test_user.account_id)[0]
    assert "private" not in str(row.details)
    assert "synthetic-password" not in str(row.details)
    assert (
        row.details["result_preview_sha256"]
        == hashlib.sha256(b"private overridden result").hexdigest()
    )
    monkeypatch.setenv(outbox.FLAG_ENV, "true")
    service.enqueue_in_transaction(db_session, "log_approval_tool_executed", **fields)
    intent = (
        db_session.query(models.AuditOutbox)
        .filter_by(account_id=test_user.account_id)
        .one()
    )
    assert "private" not in str(intent.payload)
    assert "synthetic-credential" not in str(intent.payload)
    assert "synthetic-password" not in str(intent.payload)
    # Persisted intent kwargs remain accepted by the writer, with no raw preview.
    service.log_approval_tool_executed(
        db_session, **outbox.decode_payload(intent.payload)
    )


def test_other_failure_metadata_does_not_copy_credentials(
    db_session: Session,
    test_user: models.User,
) -> None:
    service = AuditService()
    service.enable()
    fields = dict(
        account_id=test_user.account_id,
        approval_id=uuid4(),
        channel="webhook",
        status="failed",
        error="token=synthetic-token",
        extra_details={"error": "password=synthetic-password"},
    )
    service.log_approval_notification(db_session, **fields)
    row = crud_audit_log.get_by_account(db_session, account_id=test_user.account_id)[0]
    assert "synthetic-password" not in str(row.details)
    clean = outbox.sanitize_intent_fields(fields, operation="log_approval_notification")
    assert "synthetic-token" not in str(clean)
    assert "synthetic-password" not in str(clean)
