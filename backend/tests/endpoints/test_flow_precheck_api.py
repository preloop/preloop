"""Flow pre-check over the API (#2541): validation, dry run, trigger, counts."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.services import flow_precheck as engine
from preloop.services.flow_precheck_reader import PrecheckBudget
from tests.services.test_flow_precheck_engine import FakeReader, pr

FLOWS_URL = "/api/v1/flows"
REPO = "acme/app"
PR_CHECK = {"checks": [{"id": "stale_prs", "type": "open_pull_requests"}]}


@pytest.fixture
def project(db_session, test_tracker) -> Project:
    org = Organization(name="acme", identifier="acme", tracker_id=test_tracker.id)
    db_session.add(org)
    db_session.flush()
    project = Project(name="app", identifier="app", slug=REPO, organization_id=org.id)
    db_session.add(project)
    db_session.flush()
    return project


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> dict:
    holder = {"r": FakeReader()}

    async def factory(tracker: Any, budget: PrecheckBudget) -> FakeReader:
        return holder["r"]

    monkeypatch.setattr(engine, "build_reader", factory)
    return holder


def _body(project: Project, **extra: Any) -> dict:
    body = {
        "name": f"Shepherd {uuid.uuid4().hex[:8]}",
        "prompt_template": "Work on {{trigger_event.payload.precheck}}",
        "agent_type": "codex",
        "agent_config": {},
        "trigger_event_source": "schedule",
        "schedule_config": {"type": "interval", "every": 30, "unit": "minutes"},
        "trigger_project_ids": [str(project.id)],
    }
    body.update(extra)
    return body


def test_a_precheck_round_trips(client, project) -> None:
    response = client.post(FLOWS_URL, json=_body(project, precheck=PR_CHECK))
    assert response.status_code == 200, response.text
    stored = response.json()["precheck"]
    assert stored["checks"][0]["type"] == "open_pull_requests"
    assert stored["on_error"] == "fail"


@pytest.mark.parametrize(
    "precheck,detail",
    [
        (
            {
                "checks": [
                    {
                        "id": "x",
                        "type": "open_pull_requests",
                        "repositories": ["other/repo"],
                    }
                ]
            },
            "not one of the flow's projects",
        ),
        ({"checks": [{"id": "x", "type": "script"}]}, None),
        ({"checks": [{"id": "x", "type": "issues", "max_items": 99}]}, None),
    ],
)
def test_invalid_prechecks_are_refused(client, project, precheck, detail) -> None:
    response = client.post(FLOWS_URL, json=_body(project, precheck=precheck))
    assert response.status_code == 422
    if detail:
        assert detail in response.json()["detail"]


def test_an_update_that_drops_the_project_is_refused(client, project) -> None:
    created = client.post(FLOWS_URL, json=_body(project, precheck=PR_CHECK)).json()
    response = client.put(
        f"{FLOWS_URL}/{created['id']}", json={"trigger_project_ids": []}
    )
    assert response.status_code == 422
    assert "no repository" in response.json()["detail"]


def test_a_precheck_change_bumps_the_revision(client, project) -> None:
    created = client.post(FLOWS_URL, json=_body(project)).json()
    updated = client.put(
        f"{FLOWS_URL}/{created['id']}", json={"precheck": PR_CHECK}
    ).json()
    assert updated["revision"] == created["revision"] + 1


def test_the_dry_run_returns_a_decision_and_creates_nothing(
    client, db_session, project, reader
) -> None:
    reader["r"] = FakeReader(prs=[pr(101)])
    created = client.post(FLOWS_URL, json=_body(project, precheck=PR_CHECK)).json()

    response = client.post(f"{FLOWS_URL}/{created['id']}/precheck/evaluate")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "run"
    assert body["document"]["checks"]["stale_prs"]["items"][0]["number"] == 101
    assert crud_flow_execution.get_by_flow(db_session, flow_id=created["id"]) == []


def test_the_dry_run_evaluates_a_draft(client, project, reader) -> None:
    created = client.post(FLOWS_URL, json=_body(project)).json()
    url = f"{FLOWS_URL}/{created['id']}/precheck/evaluate"

    assert client.post(url).status_code == 422  # nothing stored, no draft
    response = client.post(url, json={"precheck": PR_CHECK})
    assert response.status_code == 200
    assert response.json()["action"] == "skip"

    outside = {
        "checks": [
            {"id": "x", "type": "issues", "repositories": ["other/repo"]},
        ]
    }
    assert client.post(url, json={"precheck": outside}).status_code == 422


def test_the_dry_run_needs_run_permission(client, project, reader) -> None:
    created = client.post(FLOWS_URL, json=_body(project, precheck=PR_CHECK)).json()
    with patch(
        "preloop.services.flow_trigger_service.FlowTriggerService._authorize_flow_run",
        new=AsyncMock(side_effect=PermissionError("denied by policy")),
    ):
        response = client.post(f"{FLOWS_URL}/{created['id']}/precheck/evaluate")
    assert response.status_code == 403


def test_a_manual_trigger_can_be_skipped_or_bypass_the_precheck(
    client, db_session, project, reader
) -> None:
    created = client.post(
        FLOWS_URL,
        json=_body(project, precheck={**PR_CHECK, "applies_to": ["manual"]}),
    ).json()
    url = f"{FLOWS_URL}/{created['id']}/trigger"

    skipped = client.post(url, json={})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["status"] == "SKIPPED"

    with patch(
        "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
        new=AsyncMock(return_value={"id": "x", "status": "PENDING", "flow_id": "y"}),
    ) as trigger:
        client.post(url, params={"skip_precheck": "true"}, json={})
    assert trigger.await_args.kwargs["evaluate_precheck"] is False

    detail = client.get(f"{FLOWS_URL}/executions/{skipped.json()['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "SKIPPED"
    assert detail.json()["precheck"]["decision"] == "skip"


def test_the_summary_counts_skips_in_the_window(
    client, db_session, project, test_user
) -> None:
    created = client.post(FLOWS_URL, json=_body(project, precheck=PR_CHECK)).json()
    flow = crud_flow.get(db_session, id=created["id"])
    for _ in range(4):
        crud_flow_execution.record_precheck_skip(
            db_session,
            flow_id=flow.id,
            trigger_event_details={"source": "schedule", "payload": {}},
            reason="nothing_to_do",
            summary="No open pull requests need attention",
        )
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    response = client.get(
        f"{FLOWS_URL}/summary",
        params={"include_stats": "true", "stats_since": since},
    )
    [row] = [r for r in response.json() if r["id"] == created["id"]]
    stats = row["execution_stats"]
    assert stats["skipped_count"] == 4
    assert stats["runs"] == 0
    assert stats["last_skipped_at"]
