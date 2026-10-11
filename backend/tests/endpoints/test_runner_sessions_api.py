"""Remote session HTTP API on personal runners (#1483, contract C)."""

import json
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runner_sessions
from preloop.api.endpoints.runner_sessions import (
    RemoteSessionRecord,
    RunnerSessionError,
    SessionStartPlan,
)
from preloop.models import models
from preloop.models.crud import crud_user
from preloop.models.db.session import get_db_session as get_db
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import (
    ACTION_RUNNER_SESSION_START,
    Decision,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "personal_runners"
AUDIT_KEYS = {
    "actor_user_id",
    "runner_id",
    "runner_name",
    "host",
    "harness",
    "model",
    "workspace_kind",
    "workspace_label",
}


def _inventory(**overrides: Any) -> Dict[str, Any]:
    inventory = json.loads(
        (FIXTURES / "harness_inventory_copilot_signed_in.json").read_text()
    )
    inventory = inventory.get("harness_inventory", inventory)
    for entry in inventory["entries"]:
        if entry["harness"] == "copilot_cli":
            entry.update({"sessions_enabled": True, **overrides})
    return inventory


DIRECTORIES = [
    {"id": "dir_api", "label": "api", "mode": "write", "harnesses": ["copilot_cli"]},
    {"id": "dir_docs", "label": "docs", "mode": "read_only", "harnesses": "all"},
    {"id": "dir_other", "label": "other", "mode": "write", "harnesses": ["cursor_cli"]},
]


class FakeService:
    """In-memory stand-in for the #1482 runner session service."""

    def __init__(self) -> None:
        self.records: Dict[str, RemoteSessionRecord] = {}
        self.plans: List[SessionStartPlan] = []
        self.credentials: List[Optional[Dict[str, Any]]] = []
        self.turns: List[str] = []
        self.stops: List[str] = []
        self.start_error: Optional[str] = None

    def count_active(self, db: Session, *, runner_id: str) -> int:
        return sum(
            1
            for r in self.records.values()
            if r.runner_id == runner_id and r.state in runner_sessions.ACTIVE_STATES
        )

    def list_for_runner(self, db: Session, *, runner_id: str, limit: int):
        return [r for r in self.records.values() if r.runner_id == runner_id][:limit]

    def get(self, db: Session, *, session_id: str):
        return self.records.get(session_id)

    async def start(self, db: Session, plan: SessionStartPlan) -> RemoteSessionRecord:
        if self.start_error:
            raise RunnerSessionError(self.start_error)
        self.plans.append(plan)
        self.credentials.append(plan.credential)
        record = RemoteSessionRecord(
            session_id=str(uuid4()),
            remote_session_id=plan.remote_session_id,
            runner_id=str(plan.runner.id),
            account_id=str(plan.runner.account_id),
            actor_user_id=str(plan.actor.id),
            harness=plan.harness,
            model=plan.model,
            state="requested",
            workspace=plan.workspace,
        )
        self.records[record.session_id] = record
        return record

    async def send_turn(self, db, record, *, turn_id: str, text: str) -> None:
        self.turns.append(text)
        record.turn_in_progress = True

    async def stop(self, db, record, *, mode: str):
        self.stops.append(mode)
        record.state = "stopping"
        return record


@pytest.fixture
def service():
    fake = FakeService()
    runner_sessions.register_runner_session_service(fake)
    yield fake
    runner_sessions.register_runner_session_service(None)


@pytest.fixture(autouse=True)
def _no_authorizer():
    account_hooks.register_authorizer(None)
    yield
    account_hooks.register_authorizer(None)


def _client(db: Session, user: models.User) -> TestClient:
    app = FastAPI()
    app.include_router(runner_sessions.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _member(db: Session, owner: models.User, name: str) -> models.User:
    user = crud_user.create(
        db,
        obj_in={
            "account_id": owner.account_id,
            "email": f"{name}-{uuid4().hex[:6]}@example.com",
            "username": f"{name}-{uuid4().hex[:6]}",
            "full_name": name,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _runner(
    db: Session,
    owner: models.User,
    *,
    status: str = "online",
    inventory: Optional[Dict[str, Any]] = None,
) -> models.FlowRunner:
    runner = models.FlowRunner(
        account_id=owner.account_id,
        registered_by_user_id=owner.id,
        name="laptop",
        hostname="laptop.example.com",
        token_hash=secrets.token_hex(32),
        status=status,
        capabilities={
            "harness_inventory": inventory if inventory is not None else _inventory(),
            "authorized_directories": DIRECTORIES,
        },
    )
    db.add(runner)
    db.flush()
    return runner


def _tracker(db: Session, owner: models.User, tracker_type: str) -> models.Tracker:
    tracker = models.Tracker(
        account_id=owner.account_id,
        name=f"{tracker_type}-tracker",
        tracker_type=tracker_type,
        is_active=True,
    )
    db.add(tracker)
    db.flush()
    org = models.Organization(
        name="acme", identifier=f"acme-{uuid4().hex[:6]}", tracker_id=tracker.id
    )
    db.add(org)
    db.flush()
    db.add(
        models.Project(
            name="api",
            identifier="api",
            slug="acme/api",
            organization_id=org.id,
            meta_data={"default_branch": "main"},
        )
    )
    db.flush()
    return tracker


def _audit(db: Session, runner: models.FlowRunner) -> List[models.AuditLog]:
    rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.resource_type == "runner_session")
        .order_by(models.AuditLog.timestamp)
        .all()
    )
    return [r for r in rows if (r.details or {}).get("runner_id") == str(runner.id)]


def _start_body(**overrides: Any) -> Dict[str, Any]:
    body = {
        "harness": "copilot_cli",
        "model": "auto",
        "workspace": {"kind": "authorized_directory", "id": "dir_api"},
        "first_prompt": "Summarize the open TODOs",
    }
    body.update(overrides)
    return body


def _assert_audited(
    db: Session, runner: models.FlowRunner, actor: models.User, action: str, **extra
) -> models.AuditLog:
    rows = [r for r in _audit(db, runner) if r.action == action]
    assert rows, f"no {action} audit row"
    row = rows[-1]
    assert AUDIT_KEYS <= set(row.details)
    assert row.details["actor_user_id"] == str(actor.id)
    assert row.details["host"] == "laptop.example.com"
    assert row.details["harness"] == "copilot_cli"
    for key, value in extra.items():
        assert row.details[key] == value, key
    return row


def test_owner_starts_session(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "requested"
    plan = service.plans[0]
    assert plan.workspace == {
        "kind": "authorized_directory",
        "id": "dir_api",
        "label": "api",
        "mode": "write",
    }
    assert plan.first_prompt == "Summarize the open TODOs"
    row = _assert_audited(
        db_session,
        runner,
        test_user,
        "runner_session.start_requested",
        model="auto",
        workspace_kind="authorized_directory",
        workspace_label="api",
    )
    assert row.details["remote_session_id"] == body["remote_session_id"]


def test_admin_starts_session_on_another_members_runner(db_session, test_user, service):
    member = _member(db_session, test_user, "member")
    runner = _runner(db_session, member)
    # The account's primary user administers it, with or without seeded roles.
    account = db_session.get(models.Account, test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 202, response.text
    _assert_audited(db_session, runner, test_user, "runner_session.start_requested")


def test_other_member_is_refused(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    client = _client(db_session, member)
    response = client.post(f"/api/v1/runners/{runner.id}/sessions", json=_start_body())
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_runner_owner"
    row = _assert_audited(
        db_session, runner, member, "runner_session.rejected", reason="not_runner_owner"
    )
    assert row.status == "denied"
    assert not service.plans
    assert client.get(f"/api/v1/runners/{runner.id}/session-options").status_code == 403


def test_authorizer_can_share_and_deny(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    member = _member(db_session, test_user, "member")
    seen: List[Any] = []

    def share(ctx, action, resource):
        if action != ACTION_RUNNER_SESSION_START:
            return None
        seen.append((action, ctx.attributes.get("default_allowed")))
        return Decision("allow", ("ee:runner_share",))

    account_hooks.register_authorizer(share)
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 202, response.text
    assert seen[0] == (ACTION_RUNNER_SESSION_START, False)

    # An allow without rule ids is "no opinion" and does not widen.
    account_hooks.register_authorizer(
        lambda ctx, action, res: (
            Decision("allow") if action == ACTION_RUNNER_SESSION_START else None
        )
    )
    response = _client(db_session, member).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 403

    account_hooks.register_authorizer(
        lambda ctx, action, res: (
            Decision("deny", ("ee:block",), "policy_forbidden")
            if action == ACTION_RUNNER_SESSION_START
            else None
        )
    )
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "policy_forbidden"


def test_offline_runner_is_refused(db_session, test_user, service):
    runner = _runner(db_session, test_user, status="offline")
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runner_offline"
    _assert_audited(
        db_session,
        runner,
        test_user,
        "runner_session.rejected",
        reason="runner_offline",
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"sessions_enabled": False}, "harness_not_enabled_for_sessions"),
        ({"login_state": "signed_out"}, "harness_signed_out"),
        ({"enabled": False}, "harness_disabled"),
    ],
)
def test_harness_not_session_ready_is_refused(
    db_session, test_user, service, overrides, code
):
    runner = _runner(db_session, test_user, inventory=_inventory(**overrides))
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    _assert_audited(
        db_session, runner, test_user, "runner_session.rejected", reason=code
    )


def test_runner_without_inventory_has_no_session_harnesses(
    db_session, test_user, service
):
    runner = _runner(db_session, test_user, inventory={})
    client = _client(db_session, test_user)
    options = client.get(f"/api/v1/runners/{runner.id}/session-options").json()
    assert options["harnesses"] == []
    response = client.post(f"/api/v1/runners/{runner.id}/sessions", json=_start_body())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "harness_not_enabled_for_sessions"


def test_directory_not_authorized_for_harness(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    client = _client(db_session, test_user)
    for directory in ("dir_other", "dir_missing"):
        response = client.post(
            f"/api/v1/runners/{runner.id}/sessions",
            json=_start_body(
                workspace={"kind": "authorized_directory", "id": directory}
            ),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "workspace_not_authorized"
    response = client.post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(workspace={"kind": "authorized_directory", "id": "dir_docs"}),
    )
    assert response.status_code == 202


def test_max_concurrent_is_refused(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    client = _client(db_session, test_user)
    for _ in range(runner_sessions.DEFAULT_MAX_CONCURRENT_SESSIONS):
        assert (
            client.post(
                f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
            ).status_code
            == 202
        )
    response = client.post(f"/api/v1/runners/{runner.id}/sessions", json=_start_body())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "max_concurrent_reached"
    _assert_audited(
        db_session,
        runner,
        test_user,
        "runner_session.rejected",
        reason="max_concurrent_reached",
    )


def test_runner_side_rejection_maps_to_409(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    service.start_error = "runner_offline"
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runner_offline"
    actions = [r.action for r in _audit(db_session, runner)]
    assert actions == ["runner_session.start_requested", "runner_session.rejected"]


def test_without_service_start_is_503_and_audited(db_session, test_user):
    runner_sessions.register_runner_session_service(None)
    runner = _runner(db_session, test_user)
    client = _client(db_session, test_user)
    options = client.get(f"/api/v1/runners/{runner.id}/session-options").json()
    assert options["sessions_available"] is False
    response = client.post(f"/api/v1/runners/{runner.id}/sessions", json=_start_body())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "remote_sessions_unavailable"
    _assert_audited(
        db_session,
        runner,
        test_user,
        "runner_session.rejected",
        reason="remote_sessions_unavailable",
    )
    assert client.get(f"/api/v1/runners/{runner.id}/sessions").json() == []


def test_tracker_checkout_is_stubbed_until_minting_lands(
    db_session, test_user, service, monkeypatch
):
    monkeypatch.setattr(
        runner_sessions,
        "_mint_checkout_credential",
        runner_sessions._mint_checkout_credential_stub,
    )
    runner = _runner(db_session, test_user)
    tracker = _tracker(db_session, test_user, "github")
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(
            workspace={
                "kind": "tracker_checkout",
                "tracker_id": str(tracker.id),
                "repository": "acme/api",
            }
        ),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "checkout_not_available"
    _assert_audited(
        db_session,
        runner,
        test_user,
        "runner_session.rejected",
        reason="checkout_not_available",
        workspace_kind="tracker_checkout",
        workspace_label="acme/api",
    )
    assert not service.plans


def test_tracker_checkout_credential_reaches_only_the_plan(
    db_session, test_user, service, monkeypatch
):
    token = "ghs_" + secrets.token_hex(8)
    monkeypatch.setattr(
        runner_sessions,
        "_mint_checkout_credential",
        lambda db, tracker, repository: {
            "username": "x-access-token",
            "token": token,
            "expires_at": "2026-10-16T10:00:00Z",
        },
    )
    runner = _runner(db_session, test_user)
    tracker = _tracker(db_session, test_user, "github")
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(
            workspace={
                "kind": "tracker_checkout",
                "tracker_id": str(tracker.id),
                "repository": "acme/api",
            }
        ),
    )
    assert response.status_code == 202, response.text
    assert service.credentials[0]["token"] == token
    assert token not in json.dumps(service.plans[0].workspace)
    assert service.plans[0].credential is None
    rows = _audit(db_session, runner)
    assert [r.action for r in rows] == [
        "runner_session.checkout_credential_minted",
        "runner_session.start_requested",
    ]
    for row in rows:
        assert token not in json.dumps(row.details)
    assert rows[0].details["provider"] == "github"
    assert rows[0].details["repository"] == "acme/api"


def test_checkout_from_unsupported_tracker_is_refused(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    tracker = _tracker(db_session, test_user, "jira")
    response = _client(db_session, test_user).post(
        f"/api/v1/runners/{runner.id}/sessions",
        json=_start_body(
            workspace={
                "kind": "tracker_checkout",
                "tracker_id": str(tracker.id),
                "repository": "acme/api",
            }
        ),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "checkout_source_not_supported"


def test_session_options_shape(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    tracker = _tracker(db_session, test_user, "bitbucket")
    _tracker(db_session, test_user, "jira")
    options = (
        _client(db_session, test_user)
        .get(f"/api/v1/runners/{runner.id}/session-options")
        .json()
    )
    assert options["runner_id"] == str(runner.id)
    assert options["online"] is True
    assert options["sessions_available"] is True
    assert [h["harness"] for h in options["harnesses"]] == ["copilot_cli"]
    copilot = options["harnesses"][0]
    assert copilot["available"] is True
    assert {"id": "auto", "source": "static"} in copilot["models"]
    assert [d["id"] for d in options["authorized_directories"]] == [
        "dir_api",
        "dir_docs",
        "dir_other",
    ]
    assert "path" not in json.dumps(options["authorized_directories"])
    assert options["checkout_sources"] == [
        {
            "tracker_id": str(tracker.id),
            "tracker_name": "bitbucket-tracker",
            "provider": "bitbucket_cloud",
            "repositories": [{"full_name": "acme/api", "default_branch": "main"}],
        }
    ]
    assert options["limits"] == {
        "max_concurrent": 2,
        "active": 0,
        "idle_timeout_seconds": 1800,
    }


def test_turns_list_and_stop(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    client = _client(db_session, test_user)
    session_id = client.post(
        f"/api/v1/runners/{runner.id}/sessions", json=_start_body()
    ).json()["session_id"]
    service.records[session_id].state = "idle"

    response = client.post(
        f"/api/v1/runner-sessions/{session_id}/turns", json={"text": "next step"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "queued"
    row = _assert_audited(db_session, runner, test_user, "runner_session.turn_sent")
    assert row.details["text_length"] == len("next step")
    assert "next step" not in json.dumps(row.details)

    response = client.post(
        f"/api/v1/runner-sessions/{session_id}/turns", json={"text": "again"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "turn_in_progress"

    listed = client.get(f"/api/v1/runners/{runner.id}/sessions").json()
    assert [item["session_id"] for item in listed] == [session_id]
    assert listed[0]["actor_user_id"] == str(test_user.id)

    response = client.post(
        f"/api/v1/runner-sessions/{session_id}/stop", json={"mode": "kill"}
    )
    assert response.status_code == 202
    assert response.json()["state"] == "stopping"
    assert service.stops == ["kill"]
    _assert_audited(
        db_session, runner, test_user, "runner_session.stop_requested", mode="kill"
    )


def test_member_cannot_steer_owners_session(db_session, test_user, service):
    runner = _runner(db_session, test_user)
    session_id = (
        _client(db_session, test_user)
        .post(f"/api/v1/runners/{runner.id}/sessions", json=_start_body())
        .json()["session_id"]
    )
    member_client = _client(db_session, _member(db_session, test_user, "member"))
    for path, body in (("turns", {"text": "hi"}), ("stop", {"mode": "kill"})):
        response = member_client.post(
            f"/api/v1/runner-sessions/{session_id}/{path}", json=body
        )
        assert response.status_code == 403
    assert service.turns == [] and service.stops == []
    assert (
        member_client.post(
            f"/api/v1/runner-sessions/{uuid4()}/stop", json={}
        ).status_code
        == 404
    )
