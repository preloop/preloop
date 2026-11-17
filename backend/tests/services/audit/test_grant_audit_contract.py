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


@pytest.mark.parametrize("grouped", [False, True])
def test_external_grant_reference_filter_is_account_scoped(
    audit_client: TestClient,
    db_session: Session,
    test_user: models.User,
    grouped: bool,
) -> None:
    """A decision reference also finds its tool-call group, never another account."""
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    for account_id, reference in [
        (test_user.account_id, "grant-example"),
        (test_user.account_id, "grant-other"),
        (other.id, "grant-example"),
    ]:
        correlation = f"call-{account_id}-{reference}"
        crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            action="tool_call",
            status="executed",
            resource_type="tool",
            resource_id="read_record",
            details={"correlation_id": correlation},
        )
        crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            action="approval_approved",
            status="approved",
            resource_type="approval",
            resource_id="request-example",
            details={
                "correlation_id": correlation,
                "external_result": {"grant_reference": reference},
            },
        )
    path = "/api/v1/audit-logs" + ("/grouped" if grouped else "")
    response = audit_client.get(path, params={"grant_reference": "grant-example"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    if grouped:
        assert data["groups"][0]["primary_event"]["action"] == "tool_call"
    else:
        assert (
            data["logs"][0]["details"]["external_result"]["grant_reference"]
            == "grant-example"
        )


@pytest.mark.parametrize("grouped", [False, True])
def test_audit_api_scope_error_code_filter(
    audit_client: TestClient, db_session: Session, test_user: models.User, grouped: bool
) -> None:
    """Scope refusals are selectable independently of ordinary HTTP errors."""
    for code in ["insufficient_scope", "http_403"]:
        crud_audit_log.log_action(
            db_session,
            account_id=test_user.account_id,
            action="tool_call",
            status="upstream_error",
            resource_type="tool",
            details={"error_code": code},
        )
    path = "/api/v1/audit-logs" + ("/grouped" if grouped else "")
    data = audit_client.get(path, params={"error_code": "insufficient_scope"}).json()
    assert data["total"] == 1
    entries = data["groups"] if grouped else data["logs"]
    row = entries[0]["primary_event"] if grouped else entries[0]
    assert row["details"]["error_code"] == "insufficient_scope"


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("include_last_known", [False, True])
def test_consent_search_includes_last_known_only_when_requested(
    audit_client: TestClient,
    db_session: Session,
    test_user: models.User,
    grouped: bool,
    include_last_known: bool,
) -> None:
    """A historical reference remains distinguishable from a current claim."""
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    for account, current, previous in [
        (test_user.account_id, "consent-example", None),
        (test_user.account_id, None, "consent-example"),
        (test_user.account_id, None, "consent-example-other"),
        (other.id, None, "consent-example"),
    ]:
        crud_audit_log.log_action(
            db_session,
            account_id=account,
            action="policy_deny" if current is None else "tool_call",
            status="denied",
            details={
                "grant": {"consent_ref": current, "last_known_consent_ref": previous}
            },
        )
    path = "/api/v1/audit-logs" + ("/grouped" if grouped else "")
    response = audit_client.get(
        path,
        params={
            "consent_ref": "consent-example",
            "include_last_known_consent": include_last_known,
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == (2 if include_last_known else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_only", [False, True])
@pytest.mark.parametrize("runtime_error", [False, True])
async def test_revocation_evidence_uses_stored_consent_without_caching_token(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    test_user: models.User,
    audit_client: TestClient,
    reference_only: bool,
    runtime_error: bool,
) -> None:
    """An active read followed by an RFC 7662 minimal refusal retains attribution."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import httpx
    from preloop.services import dynamic_fastmcp as gateway
    from preloop.services.grant_introspection import GrantIntrospector
    from preloop.services.policy.schema import SensitiveDataConfig

    active = True

    def respond(request: httpx.Request) -> httpx.Response:
        assert b"token=synthetic-bearer" in request.content
        return httpx.Response(
            200,
            json=(
                {"active": True, "scope": "read", "consent_ref": "consent-example"}
                if active
                else {"active": False}
            ),
        )

    introspector = GrantIntrospector(
        client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kw
        )
    )
    monkeypatch.setattr(gateway, "grant_introspector", introspector)
    from datetime import datetime
    from preloop.models.crud import crud_mcp_server

    server = crud_mcp_server.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Example server",
            "url": "https://upstream.example.com/mcp",
            "transport": "http-streaming",
            "auth_type": "bearer",
            "auth_config": {
                "token": "synthetic-bearer",
                "introspection": {
                    "endpoint": "https://issuer.example.com/introspect",
                    "client_id": "gateway",
                    "client_secret": "synthetic-secret",
                    "max_cache_ttl_seconds": 0,
                },
            },
            "updated_at": datetime(2026, 1, 1),
        },
    )
    db_proxy = MagicMock(wraps=db_session)
    db_proxy.close = MagicMock()
    monkeypatch.setattr(gateway, "get_db", lambda: iter([db_proxy]))
    monkeypatch.setattr(gateway, "_resolve_proxied_tool_server", lambda *a: server)
    snapshot, allowed = await gateway._prepare_grant_dispatch(
        str(test_user.account_id), "read_record"
    )
    user = SimpleNamespace(
        user_id=str(test_user.id),
        api_key_id="example-key",
        runtime_principal_type="managed_agent",
        runtime_principal_id="example-agent",
        runtime_principal_name="Example agent",
        managed_agent_id="example-agent",
    )
    service = audit.AuditService()
    monkeypatch.setattr(audit, "_broadcast_audit_event", lambda **kw: None)
    service.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        tool_name="read_record",
        tool_args={},
        result="executed",
        grant=allowed.binding,
    )
    if runtime_error:
        gateway._record_mcp_server_error(str(server.id), "http_403: upstream refused")
        db_session.refresh(server)
        assert server.last_error == "http_403: upstream refused"
    active = False
    snapshot, denied = await gateway._prepare_grant_dispatch(
        str(test_user.account_id), "read_record"
    )
    assert denied.deny_reason == "grant_inactive"
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {"id": "private", "scope": {"servers": ["Example server"]}}
            ]
        }
        if reference_only
        else {}
    )
    monkeypatch.setattr(
        "preloop.services.sensitive_data.storage.has_cached_config", lambda *a: True
    )
    monkeypatch.setattr(
        "preloop.services.sensitive_data.storage.cached_config", lambda *a: config
    )
    # A session proxy lets the helper release its handle without closing the fixture.
    db_proxy = MagicMock(wraps=db_session)
    db_proxy.close = MagicMock()
    monkeypatch.setattr(gateway, "get_db", lambda: iter([db_proxy]))
    monkeypatch.setattr(gateway, "outbox_enabled", lambda: False)
    monkeypatch.setattr(
        "preloop.services.policy_evaluator._log_policy_decision_async",
        lambda **kw: service.log_policy_decision(db=db_session, **kw),
    )
    await gateway._record_grant_denial(
        str(test_user.account_id),
        "read_record",
        denied,
        user_context=user,
        snapshot=snapshot,
        user_id=str(test_user.id),
    )
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="policy_deny"
    )
    assert len(rows) == 1
    fields = rows[0].details
    assert fields["api_key_id"] == user.api_key_id
    assert fields["runtime_principal_id"] == user.runtime_principal_id
    assert fields["grant"]["consent_ref"] is None
    assert ("last_known_consent_ref" in fields["grant"]) is not reference_only
    if reference_only:
        assert "consent-example" not in str(fields)
    else:
        assert fields["grant"]["last_known_consent_ref"] == "consent-example"
    for include in [False, True]:
        response = audit_client.get(
            "/api/v1/audit-logs",
            params={
                "action": "policy_deny",
                "consent_ref": "consent-example",
                "include_last_known_consent": include,
            },
        )
        assert response.status_code == 200
        assert response.json()["total"] == int(include and not reference_only)
    assert "synthetic-bearer" not in str(rows[0].details)
    assert all(
        "last_known_consent_ref" not in entry.binding
        for entry in introspector._cache.values()
    )


def test_grant_revision_changes_only_with_credential_binding(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Name, scan and diagnostic updates preserve evidence; token rotation does not."""
    from preloop.models.crud import crud_mcp_server

    server = crud_mcp_server.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Example server",
            "url": "https://upstream.example.com/mcp",
            "auth_type": "bearer",
            "auth_config": {"token": "synthetic-before"},
        },
    )
    revision = server.grant_revision
    crud_mcp_server.update(
        db_session,
        db_obj=server,
        obj_in={
            "name": "Renamed server",
            "last_error": "temporarily unavailable",
            "last_scan_at": "2026-11-17T00:00:00",
        },
    )
    assert server.grant_revision == revision
    crud_mcp_server.update(
        db_session, db_obj=server, obj_in={"auth_config": {"token": "synthetic-after"}}
    )
    assert server.grant_revision != revision
