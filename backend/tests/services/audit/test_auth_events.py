"""Authentication outcomes retain useful identities without protocol secrets."""

import io
import json
import logging
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.router import router
from preloop.models.crud import crud_account
from preloop.models.db.session import get_db_session
from preloop.services.auth_audit import record_auth_event, sanitize_auth_payload
from preloop.services.retention_export import MEMBER_AUDIT, build_period_export


def test_secret_fields_and_nested_payloads_are_dropped() -> None:
    assert sanitize_auth_payload(
        {
            "method": "password",
            "reason_code": "invalid_credentials",
            "client_secret": "synthetic-secret",
            "access_token": "synthetic-token",
            "id_token": "synthetic-id-token",
            "assertion": "synthetic-assertion",
            "SAMLResponse": "synthetic-response",
            "password": "synthetic-password",
            "provider": {"nested_secret": "synthetic-secret"},
        }
    ) == {"method": "password", "reason_code": "invalid_credentials"}


def test_unknown_identity_is_logged_without_tenant_or_raw_identifier(caplog) -> None:
    with (
        caplog.at_level(logging.INFO),
        patch("preloop.services.auth_audit.crud_audit_log.log_action") as write,
    ):
        record_auth_event(
            MagicMock(),
            action="auth.login.failed",
            details={
                "method": "social",
                "reason_code": "invalid_state",
                "code": "secret",
            },
        )
    write.assert_not_called()
    event = next(
        record.auth_event for record in caplog.records if hasattr(record, "auth_event")
    )
    assert event == {
        "action": "auth.login.failed",
        "details": {
            "method": "social",
            "reason_code": "invalid_state",
        },
    }


@pytest.mark.parametrize("path", ["/token", "/token/json"])
@pytest.mark.parametrize("outcome", ["success", "invalid_password", "policy_rejected"])
def test_password_routes_record_one_final_outcome(path: str, outcome: str) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    db = MagicMock(spec=Session)
    user = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        person_id=uuid4(),
        auth_generation=0,
    )
    person = SimpleNamespace(
        id=user.person_id, last_active_user_id=user.id, primary_user_id=user.id
    )
    app.dependency_overrides[get_db_session] = lambda: db
    gate = AsyncMock(return_value=user)
    if outcome == "policy_rejected":
        gate.side_effect = HTTPException(403, "synthetic policy refusal")
    with (
        patch(
            "preloop.api.auth.router.authenticate_user",
            AsyncMock(return_value=None if outcome == "invalid_password" else user),
        ),
        patch("preloop.api.auth.router._landing_row", gate),
        patch("preloop.api.auth.router.crud_person.resolve_login", return_value=person),
        patch(
            "preloop.services.auth_audit.crud_person.list_memberships",
            return_value=[user],
        ),
        patch("preloop.services.auth_audit.crud_audit_log.log_action") as write,
    ):
        body = {"username": "jane@example.com", "password": "synthetic-password"}
        with TestClient(app) as client:
            response = client.post(
                "/auth" + path,
                **({"data": body} if path == "/token" else {"json": body}),
            )
    assert (
        response.status_code
        == {"success": 200, "invalid_password": 401, "policy_rejected": 403}[outcome]
    )
    write.assert_called_once()
    event = write.call_args.kwargs
    assert event["action"] == (
        "auth.login.succeeded" if outcome == "success" else "auth.login.failed"
    )
    assert event["account_id"] == user.account_id
    assert event["user_id"] == user.id
    assert event["details"]["person_id"] == str(user.person_id)
    assert "synthetic-password" not in str(event)
    if outcome != "success":
        assert event["details"]["reason_code"] in {
            "invalid_credentials",
            "identity_or_policy_rejected",
        }


def test_transactional_failure_propagates_without_committing() -> None:
    db = MagicMock(spec=Session)
    with patch(
        "preloop.services.auth_audit.crud_audit_log.log_action",
        side_effect=RuntimeError("synthetic failure"),
    ) as write:
        with pytest.raises(RuntimeError):
            record_auth_event(
                db, account_id=uuid4(), action="sso.policy.changed", commit=False
            )
    assert write.call_args.kwargs["commit"] is False
    db.commit.assert_not_called()


def test_auth_events_are_searchable_and_exported_for_only_their_account(
    audit_client, db_session, test_user
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Example", "is_active": True}
    )
    start = datetime.now(UTC) - timedelta(minutes=1)
    record_auth_event(
        db_session,
        user=test_user,
        action="auth.login.failed",
        details={
            "method": "password",
            "reason_code": "invalid_credentials",
            "client_secret": "drop-this",
        },
    )
    record_auth_event(
        db_session,
        account_id=other.id,
        person_id=uuid4(),
        action="auth.login.failed",
        details={"method": "social", "provider": "other-account-marker"},
    )
    response = audit_client.get(
        "/api/v1/audit-logs", params={"action": "auth.login.failed"}
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["details"]["person_id"] == str(test_user.person_id)
    account = crud_account.get(db_session, id=test_user.account_id)
    export = build_period_export(
        db_session,
        account=account,
        start=start,
        end=datetime.now(UTC) + timedelta(minutes=1),
        sign=False,
    )
    with tarfile.open(fileobj=io.BytesIO(export.archive), mode="r:gz") as archive:
        rows = [
            json.loads(line)
            for line in archive.extractfile(MEMBER_AUDIT).read().splitlines()
        ]
    auth = [row for row in rows if row["action"] == "auth.login.failed"]
    assert len(auth) == 1
    assert "other-account-marker" not in str(rows)
    assert "drop-this" not in str(rows)


def test_scim_actor_retains_only_the_token_prefix() -> None:
    assert sanitize_auth_payload(
        {"scim_token_prefix": "synthetic-token-secret-value"}
    ) == {"scim_token_prefix": "synthetic-to"}


def test_audit_storage_failure_never_echoes_credential_material(caplog) -> None:
    db = MagicMock(spec=Session)
    with patch(
        "preloop.services.auth_audit.crud_audit_log.log_action",
        side_effect=RuntimeError("synthetic-secret-from-database-error"),
    ):
        record_auth_event(MagicMock(), account_id=uuid4(), action="auth.login.failed")
    assert "Authentication audit write failed" in caplog.text
    assert "synthetic-secret-from-database-error" not in caplog.text
