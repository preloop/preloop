"""Runner discovery has reporting authority without account read authority."""

from types import SimpleNamespace
from sqlalchemy.orm import Session
from preloop.models import models
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import agent_discovery
from preloop.models.db.session import get_db_session
from preloop.services.runner_service import hash_runner_token


@pytest.fixture
def runner_api(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Serve the real auth dependency with isolated CRUD and no user login."""
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        registered_by_user_id=uuid4(),
        token_hash=hash_runner_token("synthetic-runner-token"),
        workstation_fingerprint="a" * 64,
        ephemeral=False,
    )
    account = SimpleNamespace(id=runner.account_id, is_active=True, meta_data={})
    user = SimpleNamespace(
        id=runner.registered_by_user_id, account_id=account.id, is_active=True
    )
    monkeypatch.setattr(
        agent_discovery.crud_flow_runner,
        "get",
        lambda db, id: runner if id == runner.id else None,
    )
    monkeypatch.setattr(
        agent_discovery.crud_flow_runner,
        "bind_discovery_fingerprint",
        lambda db, runner_id, fingerprint: (
            fingerprint == runner.workstation_fingerprint
        ),
    )
    monkeypatch.setattr(agent_discovery.crud_account, "get", lambda db, id: account)
    monkeypatch.setattr(agent_discovery.crud_user, "get", lambda db, id: user)
    monkeypatch.setattr(
        agent_discovery,
        "resolve_runner_policy",
        lambda db, user: {"capabilities": ["inventory"]},
    )
    monkeypatch.setattr(
        agent_discovery.crud_account_discovery_salt,
        "get_or_create",
        lambda *args, **kwargs: "b" * 64,
    )
    app = FastAPI()
    app.include_router(agent_discovery.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: Mock()
    return TestClient(app), runner, account


def test_runner_can_get_only_its_reporting_policy(runner_api: tuple) -> None:
    """A runner token grants no discovery list or other device access."""
    client, runner, _ = runner_api
    headers = {"Authorization": "Bearer synthetic-runner-token"}
    response = client.get(
        f"/api/v1/runners/{runner.id}/discovery-policy", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    for path in (
        f"/api/v1/runners/{uuid4()}/discovery-policy",
        "/api/v1/agents/discovery-candidates",
        "/api/v1/agents/discovery-salt",
    ):
        assert client.get(path, headers=headers).status_code in (401, 403)


def test_runner_report_uses_ingestion_and_checks_device(
    runner_api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reports reuse normal candidate ingestion and cannot impersonate a device."""
    client, runner, _ = runner_api
    record = Mock(return_value={"received": 0, "created": 0, "updated": 0})
    monkeypatch.setattr(agent_discovery, "record_discovery_report", record)
    url = f"/api/v1/runners/{runner.id}/discovery-reports"
    headers = {"Authorization": "Bearer synthetic-runner-token"}
    payload = {"workstation_fingerprint": "a" * 64, "candidates": []}
    assert client.post(url, json=payload, headers=headers).status_code == 202
    assert record.call_count == 1
    payload["workstation_fingerprint"] = "c" * 64
    assert client.post(url, json=payload, headers=headers).status_code == 403
    assert record.call_count == 1


def test_account_opt_out_refuses_runner_reports(runner_api: tuple) -> None:
    """Policy is enforced again on POST, including already-running collectors."""
    client, runner, account = runner_api
    account.meta_data = {"runner_discovery": {"enabled": False}}
    headers = {"Authorization": "Bearer synthetic-runner-token"}
    policy = client.get(
        f"/api/v1/runners/{runner.id}/discovery-policy", headers=headers
    ).json()
    assert policy["enabled"] is False
    assert not policy.get("salt")
    response = client.post(
        f"/api/v1/runners/{runner.id}/discovery-reports",
        headers=headers,
        json={"workstation_fingerprint": "a" * 64, "candidates": []},
    )
    assert response.status_code == 403


def test_revoked_token_and_missing_inventory_capability(
    runner_api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capabilities and credential rotation are checked on every request."""
    client, runner, _ = runner_api
    url = f"/api/v1/runners/{runner.id}/discovery-policy"
    headers = {"Authorization": "Bearer synthetic-runner-token"}
    monkeypatch.setattr(
        agent_discovery,
        "resolve_runner_policy",
        lambda db, user: {"capabilities": ["flows"]},
    )
    assert client.get(url, headers=headers).status_code == 403
    runner.token_hash = hash_runner_token("rotated-runner-token")
    assert client.get(url, headers=headers).status_code == 401
    assert client.put(
        "/api/v1/account/runner-discovery", headers=headers, json={"enabled": False}
    ).status_code in (401, 403)


def test_policy_bounds() -> None:
    """Account policy accepts one hour through seven days or device defaults."""
    from pydantic import ValidationError

    for seconds in (3599, 604801):
        with pytest.raises(ValidationError):
            agent_discovery.RunnerDiscoveryPolicy(interval_seconds=seconds)
    for seconds in (3600, 86400, 604800, None):
        assert (
            agent_discovery.RunnerDiscoveryPolicy(
                interval_seconds=seconds
            ).interval_seconds
            == seconds
        )


def test_runner_reports_preserve_first_seen_and_evaluate_findings(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later installed agent is new; an old agent keeps its first observation."""
    from datetime import datetime, timedelta
    from preloop.models.crud import crud_discovered_agent_candidate, crud_flow_runner
    from preloop.models.crud import discovered_agent_candidate as candidate_crud

    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "registered_by_user_id": test_user.id,
            "name": "fixture-device",
            "token_hash": hash_runner_token("fixture-token"),
            "workstation_fingerprint": "a" * 64,
        },
    )
    monkeypatch.setattr(
        agent_discovery,
        "resolve_runner_policy",
        lambda db, user: {"capabilities": ["inventory"]},
    )
    emit = Mock()
    evaluate = Mock()
    monkeypatch.setattr(agent_discovery, "emit_agent_discovered", emit)
    monkeypatch.setattr(agent_discovery, "evaluate_fleet_findings", evaluate)
    app = FastAPI()
    app.include_router(agent_discovery.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    url = f"/api/v1/runners/{runner.id}/discovery-reports"
    headers = {"Authorization": "Bearer fixture-token"}
    first = datetime(2026, 11, 1)
    monkeypatch.setattr(candidate_crud, "utc_now_naive", lambda: first)
    old = {"agent_kind": "cursor", "config_path_hash": "1" * 64}
    payload = {"workstation_fingerprint": "a" * 64, "candidates": [old]}
    assert client.post(url, headers=headers, json=payload).json()["created"] == 1
    later = first + timedelta(days=1)
    monkeypatch.setattr(candidate_crud, "utc_now_naive", lambda: later)
    old["enrolled"] = True
    payload["candidates"].append({"agent_kind": "codex", "config_path_hash": "2" * 64})
    response = client.post(url, headers=headers, json=payload)
    assert response.status_code == 202
    assert response.json() == {"received": 2, "created": 1, "updated": 1}
    rows = crud_discovered_agent_candidate.list_for_account(
        db_session, account_id=test_user.account_id
    ).items
    by_kind = {row.agent_kind: row for row in rows}
    db_session.expire_all()
    assert by_kind["cursor"].first_seen_at == first
    assert by_kind["cursor"].last_seen_at == later
    assert by_kind["cursor"].reported_enrolled is True
    assert by_kind["codex"].first_seen_at == later
    assert emit.call_count == 2
    assert evaluate.call_count == 2


def test_account_policy_persists_without_replacing_other_settings(
    db_session: Session, test_user: models.User
) -> None:
    """The account opt-out survives reads and preserves unrelated metadata."""
    from preloop.models.crud import crud_account

    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    crud_account.update(
        db_session,
        db_obj=account,
        obj_in={"meta_data": {"unrelated": {"enabled": True}}},
    )
    crud_account.set_runner_discovery_policy(
        db_session,
        account_id=account.id,
        policy={"enabled": False, "interval_seconds": 3600},
    )
    db_session.expire_all()
    saved = crud_account.get(db_session, id=account.id)
    assert saved is not None
    assert saved.meta_data["unrelated"] == {"enabled": True}
    assert agent_discovery._runner_discovery_policy(saved).enabled is False
    assert agent_discovery._runner_discovery_policy(saved).interval_seconds == 3600
