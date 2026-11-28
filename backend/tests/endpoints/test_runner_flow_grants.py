"""'Always allow this flow on this runner' grants: owner-only, audited."""

from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runner_flow_grants, runners
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud import owner_consent as crud_owner_consent
from preloop.models.crud import runner_flow_grant as crud_grant
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _client(db_session: Any, user: Any) -> TestClient:
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.include_router(runner_flow_grants.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _flow(db_session: Any, account_id: Any) -> models.Flow:
    return crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Daily review",
            trigger_event_source="github",
            trigger_event_types=["push"],
            prompt_template="Review",
            agent_type="openhands",
            agent_config={},
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _audit_actions(db_session: Any, runner_id: str) -> list[str]:
    rows = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == runner_id)
        .all()
    )
    return [row.action for row in rows]


def test_grant_list_revoke_and_audit(db_session, test_user, monkeypatch) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "evaluate_fleet_findings", lambda *a, **k: None)
    flow = _flow(db_session, test_user.account_id)
    with _client(db_session, test_user) as client:
        runner_id = client.post(
            "/api/v1/runners/register", json={"name": "Example runner"}
        ).json()["id"]
        created = client.post(
            f"/api/v1/runners/{runner_id}/flow-grants",
            json={"flow_id": str(flow.id)},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["flow_name"] == "Daily review"
        assert body["runner_name"] == "Example runner"
        again = client.post(
            f"/api/v1/runners/{runner_id}/flow-grants",
            json={"flow_id": str(flow.id)},
        )
        assert again.json()["id"] == body["id"]
        assert crud_grant.active(
            db_session,
            runner_id=runner_id,
            flow_id=flow.id,
            owner_user_id=test_user.id,
        )
        listed = client.get(f"/api/v1/runners/{runner_id}/flow-grants").json()
        assert [row["id"] for row in listed] == [body["id"]]
        assert [row["id"] for row in client.get("/api/v1/runner-flow-grants").json()]

        revoked = client.delete(f"/api/v1/runner-flow-grants/{body['id']}")
        assert revoked.status_code == 200, revoked.text
        assert client.get(f"/api/v1/runners/{runner_id}/flow-grants").json() == []
        assert (
            crud_grant.active(
                db_session,
                runner_id=runner_id,
                flow_id=flow.id,
                owner_user_id=test_user.id,
            )
            is None
        )
        gone = client.delete(f"/api/v1/runner-flow-grants/{body['id']}")
        assert gone.status_code == 404
    actions = _audit_actions(db_session, runner_id)
    assert actions.count("runner_flow_grant_created") == 1
    assert actions.count("runner_flow_grant_revoked") == 1


def test_grant_from_owner_consent_approval(db_session, test_user, monkeypatch) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "evaluate_fleet_findings", lambda *a, **k: None)
    flow = _flow(db_session, test_user.account_id)
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="PENDING")
    )
    with _client(db_session, test_user) as client:
        runner_id = client.post(
            "/api/v1/runners/register", json={"name": "Example runner"}
        ).json()["id"]
        _consent, approval, _ = crud_owner_consent.get_or_create(
            db_session,
            account_id=test_user.account_id,
            owner_user_id=test_user.id,
            action="runner_flow",
            subject_id=runner_id,
            request_key=f"{execution.id}:{runner_id}:{test_user.id}",
            details={"execution_id": str(execution.id), "runner_id": runner_id},
            execution_id=str(execution.id),
        )
        wrong_runner = client.post(
            f"/api/v1/runners/{uuid4()}/flow-grants",
            json={"approval_request_id": str(approval.id)},
        )
        assert wrong_runner.status_code == 404
        created = client.post(
            f"/api/v1/runners/{runner_id}/flow-grants",
            json={"approval_request_id": str(approval.id)},
        )
        assert created.status_code == 200, created.text
        assert created.json()["flow_id"] == str(flow.id)
        assert created.json()["approval_request_id"] == str(approval.id)


def test_only_runner_owner_may_grant(db_session, test_user, monkeypatch) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "evaluate_fleet_findings", lambda *a, **k: None)
    flow = _flow(db_session, test_user.account_id)
    with _client(db_session, test_user) as client:
        runner_id = client.post(
            "/api/v1/runners/register", json={"name": "Example runner"}
        ).json()["id"]
    runner = db_session.get(models.FlowRunner, runner_id)
    runner.registered_by_user_id = None
    db_session.flush()
    with _client(db_session, test_user) as client:
        refused = client.post(
            f"/api/v1/runners/{runner_id}/flow-grants",
            json={"flow_id": str(flow.id)},
        )
    assert refused.status_code == 403


def test_list_loads_names_in_one_batch(db_session, test_user, monkeypatch) -> None:
    """Listing N grants resolves names once, not once per grant."""
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(runners, "evaluate_fleet_findings", lambda *a, **k: None)
    flows = [_flow(db_session, test_user.account_id) for _ in range(3)]
    calls: list[int] = []
    real = crud_grant.names_for

    def counting(db: Any, rows: list) -> Any:
        calls.append(len(rows))
        return real(db, rows)

    monkeypatch.setattr(crud_grant, "names_for", counting)
    with _client(db_session, test_user) as client:
        runner_id = client.post(
            "/api/v1/runners/register", json={"name": "Example runner"}
        ).json()["id"]
        for flow in flows:
            client.post(
                f"/api/v1/runners/{runner_id}/flow-grants",
                json={"flow_id": str(flow.id)},
            )
        calls.clear()
        listed = client.get("/api/v1/runner-flow-grants").json()
    assert len(listed) == 3
    assert {row["flow_name"] for row in listed} == {"Daily review"}
    assert calls == [3]
