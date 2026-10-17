"""Runner consent is owner-controlled and preserved across registrations."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from preloop.api.endpoints import runners
from preloop.models import schemas


def test_registration_consent_is_optional_and_validated() -> None:
    assert schemas.RunnerRegisterRequest().flow_execution_consent is None
    for mode in ("always", "ask_me", "never"):
        assert (
            schemas.RunnerRegisterRequest(
                flow_execution_consent=mode
            ).flow_execution_consent
            == mode
        )
    with pytest.raises(ValidationError):
        schemas.RunnerRegisterRequest(flow_execution_consent="allow")


@pytest.mark.parametrize("mode", ["always", "ask_me", "never"])
def test_only_owner_can_change_runner_consent(monkeypatch, mode) -> None:
    owner, outsider, account, runner_id = uuid4(), uuid4(), uuid4(), uuid4()
    row = SimpleNamespace(
        id=runner_id,
        registered_by_user_id=owner,
        account_id=account,
        flow_execution_consent="ask_me",
    )
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *a, **k: row)
    update = MagicMock(side_effect=lambda db, **k: row)
    monkeypatch.setattr(runners.crud_flow_runner, "update", update)
    audit = MagicMock()
    monkeypatch.setattr(runners.crud_audit_log, "log_action", audit)
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "_to_response", lambda *a: a[0])
    body = schemas.RunnerConsentUpdate(flow_execution_consent=mode)
    with pytest.raises(HTTPException) as exc:
        runners.update_runner_consent(
            runner_id,
            body,
            MagicMock(),
            SimpleNamespace(id=outsider, account_id=account),
        )
    assert exc.value.status_code == 403
    update.assert_not_called()
    runners.update_runner_consent(
        runner_id, body, MagicMock(), SimpleNamespace(id=owner, account_id=account)
    )
    assert update.call_args.kwargs["obj_in"] == {"flow_execution_consent": mode}
    audit.assert_called_once()


def test_registration_defaults_and_resume_preserves_owner_setting(
    db_session, test_user, monkeypatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session

    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "evaluate_fleet_findings", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/runners/register", json={"name": "Example runner"}
        )
        assert created.status_code == 200, created.text
        assert created.json()["flow_execution_consent"] == "ask_me"
        runner_id = created.json()["id"]
        updated = client.patch(
            f"/api/v1/runners/{runner_id}/consent",
            json={"flow_execution_consent": "never"},
        )
        assert updated.status_code == 200, updated.text
        resumed = client.post("/api/v1/runners/register", json={"runner_id": runner_id})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["flow_execution_consent"] == "never"
