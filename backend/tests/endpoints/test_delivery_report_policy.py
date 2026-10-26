"""Endpoint and CRUD tests for the Delivery Report policy (#2020)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_delivery_report_policy,
    crud_flow,
    crud_flow_execution,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import delivery_report as dr
from preloop.services.delivery_report_activation import ensure_default_report_flow

BASE = "/api/v1/delivery-report/policy"
REPO = "example-org/example-repo"


@pytest.fixture(autouse=True)
def account_owner(db_session: Session, test_user: Any) -> Any:
    """Make the caller the account's primary user, whom the OSS check admits."""
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.flush()
    return account


def test_a_user_without_the_permission_cannot_save(
    db_session: Session, test_viewer_user: Any
) -> None:
    from preloop.api.app import create_app
    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    with TestClient(app) as viewer_client:
        response = viewer_client.put(BASE, json={"overrides": {}})
    assert response.status_code == 403


def _project(db: Session, account_id: Any, slug: str = REPO) -> models.Project:
    suffix = uuid.uuid4().hex[:8]
    tracker = crud_tracker.create(
        db,
        obj_in={
            "name": f"GitHub {suffix}",
            "tracker_type": "github",
            "account_id": account_id,
            "api_key": "test_api_key",
            "url": "https://github.com",
            "is_active": True,
        },
    )
    organization = crud_organization.create(
        db,
        obj_in={
            "name": f"Org {suffix}",
            "identifier": f"org-{suffix}",
            "tracker_id": tracker.id,
            "is_active": True,
        },
    )
    return crud_project.create(
        db,
        obj_in={
            "name": f"Project {suffix}",
            "identifier": f"project-{suffix}",
            "slug": slug,
            "organization_id": organization.id,
            "is_active": True,
        },
    )


class TestAccountPolicy:
    def test_defaults_before_anything_is_saved(self, client: TestClient) -> None:
        body = client.get(BASE).json()
        assert body["level"] == "account"
        assert body["overrides"] == {}
        effective = body["effective"]
        assert effective["enabled"] is True
        assert effective["report_mode"] == "deterministic"
        assert effective["cadence"] == "weekly"
        assert effective["recipients"] == "account_owner"
        assert effective["tracker_writes"]["create_follow_up"] == "deny"
        assert effective["tracker_writes"]["close"] == "deny"
        assert (body["defaults"]["per_run_usd"], body["defaults"]["monthly_usd"]) == (
            5.0,
            25.0,
        )
        assert "per_run_usd" not in body["project_override_fields"]

    def test_save_and_read_back(self, client: TestClient) -> None:
        overrides = {
            "report_mode": "assessed",
            "per_run_usd": 3.0,
            "monthly_usd": 12.0,
            "cadence": "daily",
        }
        saved = client.put(BASE, json={"overrides": overrides})
        assert saved.status_code == 200, saved.text
        body = client.get(BASE).json()
        assert body["overrides"] == overrides
        assert body["effective"]["per_run_usd"] == 3.0
        assert body["effective"]["cadence"] == "daily"

    def test_close_allowed_is_rejected(self, client: TestClient) -> None:
        response = client.put(
            BASE, json={"overrides": {"tracker_writes": {"close": "allow"}}}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "close_not_allowed"

    def test_assessed_without_budgets_is_rejected(self, client: TestClient) -> None:
        response = client.put(BASE, json={"overrides": {"report_mode": "assessed"}})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "budget_required"


class TestProjectPolicy:
    def test_override_resolves_over_the_account(
        self, client: TestClient, db_session: Session, test_user: Any
    ) -> None:
        project = _project(db_session, test_user.account_id)
        client.put(BASE, json={"overrides": {"pr_ready_days": 3}})
        url = f"{BASE}/projects/{project.id}"
        assert client.get(url).json()["effective"]["pr_ready_days"] == 3
        saved = client.put(url, json={"overrides": {"pr_ready_days": 10}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["effective"]["pr_ready_days"] == 10
        cleared = client.delete(url)
        assert cleared.json()["effective"]["pr_ready_days"] == 3
        assert cleared.json()["overrides"] == {}

    def test_account_fields_are_refused(
        self, client: TestClient, db_session: Session, test_user: Any
    ) -> None:
        project = _project(db_session, test_user.account_id)
        response = client.put(
            f"{BASE}/projects/{project.id}",
            json={"overrides": {"tracker_writes": {"create_follow_up": "allow"}}},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "account_setting"

    def test_unknown_project_is_404(self, client: TestClient) -> None:
        assert client.get(f"{BASE}/projects/{uuid.uuid4()}").status_code == 404


class TestPolicyRun:
    def test_free_account_with_no_credit_gets_the_deterministic_report(
        self, db_session: Session, test_user: Any
    ) -> None:
        account_id = test_user.account_id
        project = _project(db_session, account_id)
        crud_delivery_report_policy.save_overrides(
            db_session,
            account_id=account_id,
            overrides={"report_mode": "assessed", "per_run_usd": 5, "monthly_usd": 25},
        )
        db_session.add(
            models.HostedCreditAccount(
                account_id=account_id,
                purchased=0,
                consumed=0,
                reserved=0,
                refunded=0,
                expired=0,
                balance=0,
            )
        )
        issue = models.Issue(
            title="Ship the export",
            status="closed",
            external_id="3",
            key=f"{REPO}#3",
            project_id=project.id,
            tracker_id=project.organization.tracker_id,
            last_updated_external=datetime(2026, 10, 21, 9, 0),
        )
        db_session.add(issue)
        db_session.flush()
        pr = models.IssueCostPullRequest(
            account_id=account_id,
            pr_key=f"https://github.com/{REPO}/pull/7",
            merged_at=datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
            state="merged",
        )
        db_session.add(pr)
        db_session.flush()
        db_session.add(
            models.IssueCostPullRequestIssue(
                account_id=account_id, pull_request_id=pr.id, issue_id=issue.id
            )
        )
        db_session.flush()
        flow = SimpleNamespace(
            id=uuid.uuid4(),
            account_id=account_id,
            agent_config={"report_kind": "delivery_report"},
        )
        run = dr.plan_policy_run(db_session, flow=flow)
        assert run.decision == "skipped_no_credit"
        assert run.calls_model is False
        report = dr.deterministic_report(
            db_session,
            flow=flow,
            execution=SimpleNamespace(id=None),
            run=run,
            window={
                "from": "2026-10-17T00:00:00+00:00",
                "to": "2026-10-24T00:00:00+00:00",
            },
        )
        assert [c["ref"] for c in report["shipped"]] == [f"{REPO}#7"]
        assert report["shipped"][0]["issues"] == [f"{REPO}#3"]
        assert [i["ref"] for i in report["closed_issues"]] == [f"{REPO}#3"]
        assert report["summary"]["assessments_skipped_reason"] == "no_credit"
        assert "no credit left" in report["report_markdown"]

    def test_a_first_run_without_a_wallet_is_the_onboarding_assessment(
        self, db_session: Session, test_user: Any
    ) -> None:
        flow = SimpleNamespace(id=uuid.uuid4(), account_id=test_user.account_id)
        run = dr.plan_policy_run(db_session, flow=flow)
        assert run.decision == "onboarding"
        assert run.payload()["policy"]["tracker_writes"]["close"] == "deny"


def test_tracker_connection_creates_one_default_report_flow(
    db_session: Session, test_user: Any
) -> None:
    """A tracker connection activates a schedule even without a model."""
    project = _project(db_session, test_user.account_id)
    assert crud_delivery_report_policy.code_repositories(
        db_session, account_id=test_user.account_id
    ) == [(REPO, str(project.id), str(project.organization.tracker_id))]
    with patch(
        "preloop.api.endpoints.flows._resolve_clone_model_binding",
        side_effect=HTTPException(status_code=422, detail="no model"),
    ):
        flow = ensure_default_report_flow(db_session, account_id=test_user.account_id)
        again = ensure_default_report_flow(db_session, account_id=test_user.account_id)
    assert flow is not None
    assert again.id == flow.id
    assert flow.is_enabled is True
    assert flow.schedule_config["type"] == "weekly"
    assert flow.schedule_config["payload"] == {"previous_result_execution_id": "last"}
    assert flow.agent_config["limits"]["max_usd"] == 5.0
    assert flow.ai_model_id is None
    run = dr.plan_policy_run(db_session, flow=flow)
    assert run.decision == "skipped_no_model"
    assert run.calls_model is False

    crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            result={"assessment": {"decision": "skipped_no_model"}},
        ),
    )
    bound_flow = SimpleNamespace(
        id=flow.id, account_id=test_user.account_id, ai_model_id=uuid.uuid4()
    )
    assert dr.plan_policy_run(db_session, flow=bound_flow).decision == "onboarding"


@pytest.mark.parametrize(
    "prior_at,expected",
    [
        (datetime(2026, 10, 20, tzinfo=UTC), "skipped_budget"),
        (datetime(2026, 9, 30, tzinfo=UTC), "assessed"),
    ],
)
def test_run_planner_reads_this_months_spend(
    db_session: Session,
    test_user: Any,
    prior_at: datetime,
    expected: str,
) -> None:
    """The scheduler decides from stored spend in the current UTC month."""
    account_id = test_user.account_id
    flow = crud_flow.create(
        db_session,
        account_id=account_id,
        flow_in=FlowCreate(
            name=f"Monthly report {uuid.uuid4().hex[:6]}",
            account_id=account_id,
            agent_type="codex",
            agent_config={"report_kind": "delivery_report"},
            prompt_template="Report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
        ),
    )
    prior = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            start_time=prior_at,
            estimated_cost=25.0,
            result={"schema": dr.REPORT_SCHEMA},
        ),
    )
    assert prior.id is not None
    crud_delivery_report_policy.save_overrides(
        db_session,
        account_id=account_id,
        overrides={"report_mode": "assessed", "per_run_usd": 5, "monthly_usd": 25},
    )
    run = dr.plan_policy_run(
        db_session,
        flow=SimpleNamespace(
            id=flow.id, account_id=account_id, ai_model_id=uuid.uuid4()
        ),
        now=datetime(2026, 10, 26, tzinfo=UTC),
    )
    assert run.decision == expected


def test_saving_the_account_policy_updates_the_report_flow(
    client: TestClient, db_session: Session, test_user: Any
) -> None:
    from preloop.models.crud import crud_flow
    from preloop.models.schemas.flow import FlowCreate

    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"delivery-report-{uuid.uuid4().hex[:6]}",
            prompt_template="report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            schedule_config={
                "type": "weekly",
                "days": ["mon"],
                "at": "07:00",
                "timezone": "UTC",
            },
            agent_type="codex",
            agent_config={"report_kind": "delivery_report", "limits": {"max_usd": 5.0}},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=str(test_user.account_id),
        ),
    )
    response = client.put(
        BASE,
        json={
            "overrides": {
                "report_mode": "assessed",
                "per_run_usd": 2.5,
                "monthly_usd": 10.0,
                "cadence": "daily",
                "at": "08:30",
            }
        },
    )
    assert response.status_code == 200, response.text
    db_session.refresh(flow)
    assert flow.agent_config["limits"]["max_usd"] == 2.5
    assert flow.schedule_config["type"] == "daily"
    assert flow.schedule_config["at"] == "08:30"
    assert flow.schedule_config["payload"] == {"previous_result_execution_id": "last"}
    assert flow.is_enabled is True


def test_a_bad_timezone_is_refused(client: TestClient) -> None:
    response = client.put(BASE, json={"overrides": {"timezone": "Mars/Olympus"}})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_policy"


def test_leaving_manual_turns_the_schedule_back_on(
    client: TestClient, db_session: Session, test_user: Any
) -> None:
    from preloop.models.crud import crud_flow
    from preloop.models.schemas.flow import FlowCreate

    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"delivery-report-{uuid.uuid4().hex[:6]}",
            prompt_template="report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            schedule_config={
                "type": "weekly",
                "days": ["mon"],
                "at": "07:00",
                "timezone": "UTC",
            },
            agent_type="codex",
            agent_config={"report_kind": "delivery_report"},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=str(test_user.account_id),
        ),
    )
    assert (
        client.put(BASE, json={"overrides": {"cadence": "manual"}}).status_code == 200
    )
    db_session.refresh(flow)
    assert flow.is_enabled is False
    assert (
        client.put(BASE, json={"overrides": {"cadence": "weekly"}}).status_code == 200
    )
    db_session.refresh(flow)
    assert flow.is_enabled is True
