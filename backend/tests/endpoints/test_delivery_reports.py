"""Endpoint tests for the console's Delivery Report pages (#2022)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.delivery_report import finalize_delivery_report

BASE = "/api/v1/delivery-reports"
T0 = datetime(2026, 10, 24, 7, 0)


def _flow(db: Session, account_id: Any, *, report: bool) -> Any:
    config = {"report_kind": "delivery_report"} if report else {}
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"flow-{uuid.uuid4().hex[:8]}",
            prompt_template="report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
            agent_type="openhands",
            agent_config=config,
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _report(shipped: int = 1) -> dict[str, Any]:
    return finalize_delivery_report(
        {
            "status": "success",
            "window": {"from": "2026-10-17T07:00:00Z", "to": "2026-10-24T07:00:00Z"},
            "shipped": [
                {
                    "ref": f"example/app#{n}",
                    "project": "example/app",
                    "issues": ["example/app#100"] if n == 0 else [],
                    "purpose": {},
                }
                for n in range(shipped)
            ],
            "closed_issues": [
                {
                    "ref": "example/app#100",
                    "updated_at": "2026-10-20T10:00:00Z",
                    "closed_by": ["example/app#0"],
                    "purpose": {"criteria": [{"text": "fast", "met": None}]},
                }
            ],
            "follow_ups": [{"title": "Add a test"}],
            "reopen_requests": [
                {
                    "ref": "example/app#100",
                    "revision": "abc123",
                    "evidence": ["comment measures 3s"],
                }
            ],
        },
        cost_usd=1.5,
        total_tokens=1000,
    )


def _run(
    db: Session, flow: Any, *, hours: int, result: Optional[dict[str, Any]]
) -> Any:
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="RUNNING", trigger_event_details={}
        ),
    )
    execution.status = "SUCCEEDED"
    execution.start_time = T0 + timedelta(hours=hours)
    execution.result = result
    db.commit()
    return execution


def test_list_returns_only_report_runs_newest_first_with_server_total(
    client, db_session: Session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id, report=True)
    other = _flow(db_session, test_user.account_id, report=False)
    older = _run(db_session, flow, hours=0, result=_report(shipped=2))
    newer = _run(db_session, flow, hours=24, result=_report(shipped=3))
    _run(db_session, flow, hours=30, result=None)
    _run(db_session, other, hours=1, result=_report())

    response = client.get(BASE, params={"limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [row["id"] for row in body["items"]] == [str(newer.id)]
    row = body["items"][0]
    assert row["shipped"] == 3
    assert row["closed"] == 1
    # The closed issue's one criterion has no evidence: an evidence gap.
    assert row["closed_unverified"] == 1
    assert row["proposals"] == 2
    assert row["cost_usd"] == 1.5
    assert row["projects"] == ["example/app"]
    assert row["window"] == {
        "from": "2026-10-17T07:00:00Z",
        "to": "2026-10-24T07:00:00Z",
    }

    page_two = client.get(BASE, params={"limit": 1, "offset": 1}).json()
    assert [row["id"] for row in page_two["items"]] == [str(older.id)]


def test_detail_returns_the_report_with_actions_on(
    client, db_session: Session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id, report=True)
    run = _run(db_session, flow, hours=0, result=_report())

    response = client.get(f"{BASE}/{run.id}")
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["report"]["summary"]
    assert summary["linkage"] == {"linked": 1, "total": 1}
    assert summary["criteria_availability"] == {"with_criteria": 1, "total": 1}
    assert summary["evidence_availability"] == {"with_evidence": 0, "total": 1}
    assert body["actions"] == {"open_issue": True, "reopen": True, "dismiss": True}
    assert body["stale_reopen_requests"] == {}


def test_detail_is_404_for_other_flows_and_other_accounts(
    client, db_session: Session, test_user
) -> None:
    other = _flow(db_session, test_user.account_id, report=False)
    not_a_report = _run(db_session, other, hours=0, result=_report())
    assert client.get(f"{BASE}/{not_a_report.id}").status_code == 404

    stranger = crud_account.create(
        db_session,
        obj_in={"organization_name": "stranger", "is_active": True, "meta_data": {}},
    )
    foreign_flow = _flow(db_session, stranger.id, report=True)
    foreign = _run(db_session, foreign_flow, hours=0, result=_report())
    assert client.get(f"{BASE}/{foreign.id}").status_code == 404
    assert client.get(BASE).json()["total"] == 0
    assert client.get(f"{BASE}/{uuid.uuid4()}").status_code == 404


def test_attention_only_includes_proposals_that_need_a_person(
    client, db_session: Session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id, report=True)
    report = _report()
    report["follow_ups"] = [
        {"title": "Decide this"},
        {"title": "Run applied", "issue_url": "https://example.com/issues/1"},
        {"title": "Dismissed"},
        {"title": "In flight"},
        {"title": "Applied"},
        {"title": "Check the tracker"},
        {"title": "Abandoned claim"},
        {"title": "Claim with no timestamp"},
    ]
    report["proposal_actions"] = {
        "follow_up:2": {"status": "dismissed"},
        "follow_up:3": {"status": "pending", "at": datetime.now(UTC).isoformat()},
        "follow_up:4": {"status": "applied"},
        "follow_up:5": {"status": "unconfirmed"},
        "follow_up:6": {
            "status": "pending",
            "at": (datetime.now(UTC) - timedelta(minutes=16)).isoformat(),
        },
        "follow_up:7": {"status": "pending"},
    }
    older = _run(db_session, flow, hours=0, result=report)
    # The older proposal must survive more than one collection page.
    for i in range(26):
        _run(db_session, flow, hours=i + 1, result={"shipped": [], "closed_issues": []})
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = _flow(db_session, foreign_account.id, report=True)
    _run(db_session, foreign, hours=50, result=_report())
    body = client.get(f"{BASE}/attention").json()
    assert [item["id"] for item in body] == [str(older.id)]
    assert [p["key"] for p in body[0]["proposals"]] == [
        "reopen:example/app#100",
        "follow_up:0",
        "follow_up:5",
        "follow_up:6",
        "follow_up:7",
    ]
    assert body[0]["proposals"][-1]["unconfirmed"] is True
    fingerprint = body[0]["fingerprint"]
    assert len(fingerprint) == 64
    # Deciding one proposal changes the dismissal fingerprint.
    crud_flow_execution.set_delivery_report_action(
        db_session,
        execution_id=older.id,
        key="follow_up:0",
        record={"kind": "dismiss", "status": "dismissed", "reason": "Expected"},
    )
    assert client.get(f"{BASE}/attention").json()[0]["fingerprint"] != fingerprint

    detail = client.get(f"{BASE}/{older.id}").json()["report"]["proposal_actions"]
    assert detail["follow_up:3"]["status"] == "pending"
    assert detail["follow_up:6"]["status"] == "unconfirmed"
    assert detail["follow_up:7"]["status"] == "unconfirmed"
    db_session.refresh(older)
    assert older.result["proposal_actions"]["follow_up:6"]["status"] == "pending"


def test_attention_query_bounds_candidates_after_filtering_settled_reports(
    db_session: Session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id, report=True)
    oldest = _run(db_session, flow, hours=0, result={"follow_ups": [{"title": "Old"}]})
    newest = _run(db_session, flow, hours=1, result={"follow_ups": [{"title": "New"}]})
    for index in range(3):
        _run(
            db_session,
            flow,
            hours=index + 2,
            result={
                "follow_ups": [{"title": "Settled"}],
                "proposal_actions": {"follow_up:0": {"status": "dismissed"}},
            },
        )
    rows = crud_flow_execution.delivery_reports_with_proposals(
        db_session, account_id=str(test_user.account_id), limit=1
    )
    assert [row.id for row in rows] == [newest.id]
    rows = crud_flow_execution.delivery_reports_with_proposals(
        db_session, account_id=str(test_user.account_id), limit=2
    )
    assert [row.id for row in rows] == [newest.id, oldest.id]


def test_attention_is_quiet_for_verdicts_and_run_applied_proposals(
    client, db_session: Session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id, report=True)
    report = _report()
    report["reopen_requests"][0]["applied"] = True
    report["follow_ups"][0]["issue_url"] = "https://example.com/issues/3"
    _run(db_session, flow, hours=0, result=report)
    assert report["summary"]["closed_unverified"] == 1
    assert client.get(f"{BASE}/attention").json() == []


def test_execution_pr_uses_ledger_and_newest_assessment_at_current_revision(
    client, db_session: Session, test_user
) -> None:
    from preloop.models.crud.assessment import crud_assessment
    from preloop.models.crud.issue_cost import crud_issue_cost

    flow = _flow(db_session, test_user.account_id, report=False)
    url = "https://github.com/example/app/pull/10"
    run = _run(db_session, flow, hours=0, result={"pr_url": url, "verdict": "covered"})
    pull = crud_issue_cost.get_or_create_pull_request(
        db_session, account_id=test_user.account_id, pr_key=url
    )
    pull.state = "merged"
    pull.head_sha = "current"
    db_session.flush()
    for revision, verdict, hours in [
        ("current", "partial", 1),
        ("current", "unverified", 2),
        ("old", "covered", 3),
    ]:
        crud_assessment.record(
            db_session,
            account_id=test_user.account_id,
            source="audit",
            issue_cost_pull_request_id=pull.id,
            revision=revision,
            purpose_verdict=verdict,
            assessed_at=T0 + timedelta(hours=hours),
        )
    response = client.get(f"/api/v1/flows/executions/{run.id}")
    assert response.status_code == 200, response.text
    assert response.json()["pull_request"] == {
        "url": url,
        "state": "merged",
        "revision": "current",
        "purpose_verdict": "unverified",
    }
    pull.head_sha = "new"
    db_session.flush()
    assert (
        client.get(f"/api/v1/flows/executions/{run.id}").json()["pull_request"][
            "purpose_verdict"
        ]
        is None
    )
    run.result = {}
    db_session.flush()
    assert (
        client.get(f"/api/v1/flows/executions/{run.id}").json()["pull_request"] is None
    )
