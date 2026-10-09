"""Real transactional persistence, policy changes, retries and round-robin jobs."""

from typing import Any

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_issue_cost,
    crud_organization,
    crud_project,
    crud_tracker,
    readiness,
)
from preloop.schemas.readiness import (
    GateEvidence,
    ReadinessObservation,
    ReadinessPolicy,
    TicketCreationEvidence,
)

T0 = datetime(2026, 1, 1, 8, tzinfo=UTC)


def seed(db: Session, account_id: Any) -> Any:
    jira = crud_tracker.create(
        db,
        obj_in={
            "name": "Synthetic Jira",
            "tracker_type": "jira",
            "account_id": account_id,
            "api_key": "synthetic",
            "url": "https://example.atlassian.net",
            "is_active": True,
        },
    )
    host = crud_tracker.create(
        db,
        obj_in={
            "name": "Synthetic Bitbucket",
            "tracker_type": "bitbucket",
            "account_id": account_id,
            "api_key": "synthetic",
            "url": "https://bitbucket.org",
            "is_active": True,
        },
    )
    org = crud_organization.create(
        db,
        obj_in={
            "name": "Synthetic",
            "identifier": uuid4().hex,
            "tracker_id": jira.id,
            "is_active": True,
        },
    )
    project = crud_project.create(
        db,
        obj_in={
            "name": "Synthetic",
            "identifier": uuid4().hex,
            "organization_id": org.id,
            "settings": {
                "repository_bindings": [
                    {"tracker_id": str(host.id), "repository": "example/repo"}
                ]
            },
        },
    )
    rollup = crud_issue_cost.get_or_create_rollup(
        db, account_id=account_id, tracker_id=jira.id, issue_key="EXAMPLE-1"
    )
    rollup.project_id = project.id
    pr = crud_issue_cost.get_or_create_pull_request(
        db,
        account_id=account_id,
        pr_key="https://bitbucket.org/example/repo/pull-requests/1",
    )
    pr.rollup_id = rollup.id
    selected = ReadinessPolicy(
        version=uuid4(),
        required_build_keys=(),
        minimum_approvals=0,
        changes_requests_block=False,
        unresolved_tasks_block=False,
    )
    readiness.activate_policy(
        db, account_id=account_id, project_id=project.id, policy=selected
    )
    db.flush()
    return project, rollup, pr, jira, host, selected


def observation(
    account_id: Any,
    host_id: Any,
    selected: Any,
    completed: Any = T0 + timedelta(hours=3),
    state: Any = "ready",
) -> Any:
    return ReadinessObservation(
        observation_id=uuid4(),
        account_id=account_id,
        tracker_id=host_id,
        repository="example/repo",
        pr_id=1,
        source_sha="a" * 40,
        target_sha="b" * 40,
        policy_version=selected.version,
        started_at=completed - timedelta(seconds=10),
        completed_at=completed,
        state=state,
        coverage="complete",
        gates=tuple(
            GateEvidence(
                name=name,
                state="fail" if name == "conflict" and state != "ready" else "pass",
                source="fixture",
                retrieved_at=completed,
                source_sha="a" * 40,
                target_sha="b" * 40,
            )
            for name in ("open", "non_draft", "approvals", "conflict")
        ),
    )


def test_policy_cas_replays_regression_and_new_series(
    db_session: Any, test_user: Any
) -> Any:
    db = db_session
    account = test_user.account_id
    project, rollup, pr, jira, host, selected = seed(db, account)
    first = observation(account, host.id, selected)
    readiness.persist_observation(
        db, account_id=account, pr_record_id=pr.id, observation=first
    )
    readiness.persist_observation(
        db, account_id=account, pr_record_id=pr.id, observation=first
    )
    latest = observation(
        account,
        host.id,
        selected,
        completed=first.completed_at + timedelta(minutes=2),
        state="not_ready",
    )
    readiness.persist_observation(
        db, account_id=account, pr_record_id=pr.id, observation=latest
    )
    creation = TicketCreationEvidence(
        created_at=T0, tracker_id=jira.id, issue_key="EXAMPLE-1", retrieved_at=T0
    )
    readiness.persist_ticket_creation(
        db, account_id=account, rollup_id=rollup.id, evidence=creation
    )
    ticket, series, reason = readiness.report_evidence(
        db, account_id=account, rollup_id=rollup.id
    )
    assert ticket.created_at == T0
    assert reason is None
    assert series[0][0].observation_id == first.observation_id
    assert series[0][1].state == "not_ready"
    revised = selected.model_copy(update={"version": uuid4()})
    readiness.activate_policy(
        db, account_id=account, project_id=project.id, policy=revised
    )
    with pytest.raises(readiness.PolicyChangedError, match="policy_changed"):
        readiness.persist_observation(
            db, account_id=account, pr_record_id=pr.id, observation=first
        )
    ticket, series, reason = readiness.report_evidence(
        db, account_id=account, rollup_id=rollup.id
    )
    assert series == [(None, None)]
    with pytest.raises(ValueError, match="cross_account"):
        readiness.persist_observation(
            db, account_id=uuid4(), pr_record_id=pr.id, observation=first
        )


def test_scheduler_dedup_lease_and_cursor_over_100(
    db_session: Any, test_user: Any
) -> Any:
    db = db_session
    account = test_user.account_id
    project, rollup, pr, jira, host, selected = seed(db, account)
    for number in range(2, 104):
        other = crud_issue_cost.get_or_create_pull_request(
            db,
            account_id=account,
            pr_key=f"https://bitbucket.org/example/repo/pull-requests/{number}",
        )
        other.rollup_id = rollup.id
    assert readiness.reconcile(db, now=T0) == 100
    jobs = list(db.query(models.ReadinessJob).filter_by(account_id=account))
    assert len(jobs) == 100
    readiness.reconcile(db, now=T0 + timedelta(minutes=5))
    assert db.query(models.ReadinessJob).filter_by(account_id=account).count() == 103
    claimed = readiness.claim(db, now=T0)
    assert claimed is not None
    token = claimed.lease_token
    readiness.schedule(
        db, account_id=account, pr_id=claimed.pr_id, now=T0 + timedelta(seconds=2)
    )
    assert claimed.lease_token == token
    assert not readiness.finish(db, job_id=claimed.id, token=uuid4(), now=T0)
    assert readiness.finish(db, job_id=claimed.id, token=token, now=T0)
    assert claimed.due_at == T0 + timedelta(seconds=2)
    _, series, reason = readiness.report_evidence(
        db, account_id=account, rollup_id=rollup.id
    )
    assert reason == "ambiguous_pr"


def test_policy_endpoint_account_authorization_and_export(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any
) -> Any:
    import csv
    import io
    from preloop.config import settings

    monkeypatch.setattr(settings, "ticket_readiness_enabled", True)
    account = test_user.account_id
    project, rollup, pr, jira, host, selected = seed(db_session, account)
    response = client.get(f"/api/v1/projects/{project.id}/readiness-policy")
    assert response.status_code == 200
    assert response.json()["version"] == str(selected.version)
    body = {
        "required_build_keys": [],
        "minimum_approvals": 0,
        "changes_requests_block": False,
        "unresolved_tasks_block": False,
    }
    response = client.put(f"/api/v1/projects/{uuid4()}/readiness-policy", json=body)
    assert response.status_code == 404
    response = client.put(
        f"/api/v1/projects/{project.id}/readiness-policy", json={"minimum_approvals": 0}
    )
    assert response.status_code == 422
    response = client.put(f"/api/v1/projects/{project.id}/readiness-policy", json=body)
    assert response.status_code == 200
    revised = ReadinessPolicy.model_validate(response.json())
    assert revised.version != selected.version
    first = observation(account, host.id, revised)
    readiness.persist_observation(
        db_session, account_id=account, pr_record_id=pr.id, observation=first
    )
    readiness.persist_ticket_creation(
        db_session,
        account_id=account,
        rollup_id=rollup.id,
        evidence=TicketCreationEvidence(
            created_at=T0, tracker_id=jira.id, issue_key="EXAMPLE-1", retrieved_at=T0
        ),
    )
    rollup.run_count = 1
    rollup.first_event_at = T0 + timedelta(hours=1)
    rollup.pr_opened_at = T0 + timedelta(hours=2)
    db_session.flush()
    response = client.get("/api/v1/cost/by-issue/export?format=json")
    assert response.status_code == 200
    row = response.json()["issues"][0]
    assert row["ticket_to_observed_ready_hours"] == 3
    assert row["first_event_to_pr_opened_hours"] == 1
    assert row["readiness_scope"] == "configured_policy"
    assert row["readiness_policy_version"] == str(revised.version)
    response = client.get("/api/v1/cost/by-issue/export?format=csv")
    csv_row = list(csv.DictReader(io.StringIO(response.text)))[0]
    assert csv_row["ticket_to_observed_ready_hours"] == "3.0"
    assert csv_row["readiness_scope"] == row["readiness_scope"]
    assert csv_row["readiness_policy_version"] == row["readiness_policy_version"]


@pytest.mark.asyncio
async def test_actual_gate_reads_and_policy_change_reject_ready(
    db_session: Any, test_user: Any
) -> Any:
    from tests.services.readiness.test_bitbucket import tracker
    from preloop.services.readiness.bitbucket import observe_bitbucket

    account = test_user.account_id
    project, rollup, pr, jira, host, selected = seed(db_session, account)
    revised = selected.model_copy(update={"version": uuid4()})

    class MovingPolicyProbe:
        async def assess(
            self, repository: str, source_sha: str, target_sha: str
        ) -> GateEvidence:
            readiness.activate_policy(
                db_session, account_id=account, project_id=project.id, policy=revised
            )
            return GateEvidence(
                name="conflict",
                state="pass",
                source="fixture",
                retrieved_at=datetime.now(UTC),
                source_sha=source_sha,
                target_sha=target_sha,
                strategy="ort",
            )

    client, _ = tracker()
    result = await observe_bitbucket(
        client,
        MovingPolicyProbe(),
        account_id=account,
        tracker_id=host.id,
        repository="example/repo",
        pr_id=1,
        policy=selected,
    )
    assert result.state == "ready"
    with pytest.raises(readiness.PolicyChangedError, match="policy_changed"):
        readiness.persist_observation(
            db_session, account_id=account, pr_record_id=pr.id, observation=result
        )
    _, series, _ = readiness.report_evidence(
        db_session, account_id=account, rollup_id=rollup.id
    )
    assert series == [(None, None)]
    assert (
        readiness.get_observation(
            db_session, account_id=account, observation_id=result.observation_id
        )
        is None
    )


def test_incomplete_ready_is_rejected_and_observation_identity_scoped(
    db_session: Any, test_user: Any
) -> Any:
    account = test_user.account_id
    project, rollup, pr, jira, host, selected = seed(db_session, account)
    ready = observation(account, host.id, selected)
    incomplete = ready.model_copy(update={"gates": ready.gates[-1:]})
    with pytest.raises(ValueError, match="incomplete_gate_evidence"):
        readiness.persist_observation(
            db_session, account_id=account, pr_record_id=pr.id, observation=incomplete
        )
    readiness.persist_observation(
        db_session, account_id=account, pr_record_id=pr.id, observation=ready
    )
    assert (
        readiness.get_observation(
            db_session, account_id=uuid4(), observation_id=ready.observation_id
        )
        is None
    )
    assert (
        readiness.get_observation(
            db_session, account_id=account, observation_id=ready.observation_id
        ).source_sha
        == ready.source_sha
    )
