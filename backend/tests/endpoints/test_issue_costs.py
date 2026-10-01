"""Endpoint tests for cost and cycle time per tracker issue (#958)."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import issue_cost_rollup

BASE = "/api/v1/cost/by-issue"
REPO = "example-org/example-repo"
PR_URL = f"https://github.com/{REPO}/pull/7"
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _seed(db: Session, account_id: Any, *, record: bool = True) -> dict[str, Any]:
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
    project = crud_project.create(
        db,
        obj_in={
            "name": f"Project {suffix}",
            "identifier": f"project-{suffix}",
            "slug": REPO,
            "organization_id": organization.id,
            "is_active": True,
        },
    )
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"implement-{suffix}",
            prompt_template="work",
            trigger_event_source="github",
            trigger_event_types=["issue_labeled"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    details = {
        "source": "github",
        "tracker_id": str(tracker.id),
        "project_id": str(project.id),
        "payload": {
            "issue": {"number": 5, "title": "Export", "html_url": "https://x/5"},
            "repository": {"full_name": REPO},
        },
    }
    executions = []
    for index, (details_in, result) in enumerate(
        [
            (details, None),
            (details, {"pr_url": PR_URL}),
            ({"source": "schedule", "payload": {}}, None),
        ]
    ):
        execution = crud_flow_execution.create(
            db,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id, status="RUNNING", trigger_event_details=details_in
            ),
        )
        execution.status = "SUCCEEDED"
        execution.start_time = (T0 + timedelta(hours=index)).replace(tzinfo=None)
        execution.end_time = (T0 + timedelta(hours=index, minutes=30)).replace(
            tzinfo=None
        )
        execution.total_tokens = 100 * (index + 1)
        execution.estimated_cost = Decimal("0.2500")
        execution.result = result
        db.commit()
        if record:
            issue_cost_rollup.record_execution_finished(db, execution)
            db.commit()
        executions.append(execution)
    return {"flow": flow, "project": project, "executions": executions}


def test_list_returns_issue_rows_summaries_and_unassigned(
    client, db_session: Session, test_user
) -> None:
    seeded = _seed(db_session, test_user.account_id)

    response = client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert [row["issue_key"] for row in body["issues"]] == [f"{REPO}#5"]
    row = body["issues"][0]
    assert row["run_count"] == 2
    assert row["total_tokens"] == 300
    assert row["estimated_cost"] == 0.5
    assert row["pr_url"] == PR_URL
    assert row["project_id"] == str(seeded["project"].id)
    assert row["approved_to_merged_hours"] is None
    assert row["execution_ids"] is None
    # Every seeded run was priced, so the row is complete and its subtotal is
    # an attributable total (#1057).
    assert row["cost_coverage"] == "complete"
    assert row["known_cost_run_count"] == 2
    assert row["unknown_cost_run_count"] == 0
    assert row["attributed_cost_usd"] == 0.5
    assert body["by_project"][0]["estimated_cost"] == 0.5
    assert body["by_project"][0]["cost_coverage"] == "complete"
    assert body["by_flow"][0]["id"] == str(seeded["flow"].id)
    assert body["by_flow"][0]["attributed_cost_usd"] == 0.5
    assert body["unassigned"]["run_count"] == 1
    assert body["unassigned"]["cost_coverage"] == "complete"
    assert body["unassigned"]["attributed_cost_usd"] == 0.25
    # The plain report carries the unassigned totals, not the rows.
    assert body["unassigned"]["executions"] == []
    assert body["truncated"] is False


def test_list_filters_and_validates_period(
    client, db_session: Session, test_user
) -> None:
    _seed(db_session, test_user.account_id)
    late = client.get(BASE, params={"start_date": "2026-09-02T00:00:00Z"})
    assert late.status_code == 200
    assert late.json()["issues"] == []

    other_flow = client.get(BASE, params={"flow_id": str(uuid.uuid4())})
    assert other_flow.json()["issues"] == []

    bad = client.get(
        BASE,
        params={
            "start_date": "2026-09-02T00:00:00Z",
            "end_date": "2026-09-01T00:00:00Z",
        },
    )
    assert bad.status_code == 422


def test_mixed_naive_and_aware_period_is_validated_not_500(
    client, db_session: Session, test_user
) -> None:
    _seed(db_session, test_user.account_id)
    reversed_mixed = client.get(
        BASE,
        params={
            "start_date": "2026-09-02T00:00:00",
            "end_date": "2026-09-01T00:00:00Z",
        },
    )
    assert reversed_mixed.status_code == 422

    ordered_mixed = client.get(
        BASE,
        params={
            "start_date": "2026-08-31T00:00:00",
            "end_date": "2026-09-02T00:00:00Z",
        },
    )
    assert ordered_mixed.status_code == 200
    assert len(ordered_mixed.json()["issues"]) == 1

    rebuild = client.post(
        f"{BASE}/rebuild",
        json={
            "start_date": "2026-09-02T00:00:00",
            "end_date": "2026-09-01T00:00:00Z",
        },
    )
    assert rebuild.status_code == 422
    rebuild_ok = client.post(
        f"{BASE}/rebuild",
        json={
            "start_date": "2026-08-31T00:00:00",
            "end_date": "2026-09-02T00:00:00Z",
        },
    )
    assert rebuild_ok.status_code == 200


def test_executions_of_one_issue(client, db_session: Session, test_user) -> None:
    seeded = _seed(db_session, test_user.account_id)
    rollup_id = client.get(BASE).json()["issues"][0]["id"]

    response = client.get(f"{BASE}/{rollup_id}/executions")

    assert response.status_code == 200
    rows = response.json()
    assert [row["execution_id"] for row in rows] == [
        str(execution.id) for execution in seeded["executions"][:2]
    ]
    assert rows[0]["flow_name"] == seeded["flow"].name
    assert rows[0]["status"] == "SUCCEEDED"
    assert rows[0]["end_time"] is not None


def test_unassigned_executions_drill_down(
    client, db_session: Session, test_user
) -> None:
    seeded = _seed(db_session, test_user.account_id)
    stranger = crud_account.create(
        db_session,
        obj_in={"organization_name": "stranger", "is_active": True, "meta_data": {}},
    )
    _seed(db_session, stranger.id)

    response = client.get(f"{BASE}/unassigned/executions")

    assert response.status_code == 200
    rows = response.json()
    assert [row["execution_id"] for row in rows] == [str(seeded["executions"][2].id)]
    assert rows[0]["link"] == "unassigned"
    assert rows[0]["flow_name"] == seeded["flow"].name
    unassigned = client.get(BASE).json()["unassigned"]
    assert sum(row["estimated_cost"] for row in rows) == unassigned["estimated_cost"]

    later = client.get(
        f"{BASE}/unassigned/executions",
        params={"start_date": "2026-09-02T00:00:00Z"},
    )
    assert later.status_code == 200 and later.json() == []
    reversed_period = client.get(
        f"{BASE}/unassigned/executions",
        params={
            "start_date": "2026-09-02T00:00:00Z",
            "end_date": "2026-09-01T00:00:00Z",
        },
    )
    assert reversed_period.status_code == 422


def test_executions_of_another_accounts_issue_is_404(
    client, db_session: Session, test_user
) -> None:
    stranger = crud_account.create(
        db_session,
        obj_in={"organization_name": "stranger", "is_active": True, "meta_data": {}},
    )
    _seed(db_session, stranger.id)
    report = issue_cost_rollup.build_report(db_session, account_id=stranger.id)

    response = client.get(f"{BASE}/{report.issues[0].id}/executions")

    assert response.status_code == 404
    assert client.get(BASE).json()["issues"] == []


def test_csv_export_matches_the_table(client, db_session: Session, test_user) -> None:
    _seed(db_session, test_user.account_id)
    table = client.get(BASE).json()

    response = client.get(f"{BASE}/export", params={"format": "csv"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "issue-costs.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows[0]["issue_key"] == table["issues"][0]["issue_key"]
    assert float(rows[0]["estimated_cost"]) == table["issues"][0]["estimated_cost"]
    assert int(rows[0]["run_count"]) == table["issues"][0]["run_count"]
    assert rows[-1]["issue_key"] == issue_cost_rollup.UNASSIGNED_ISSUE_KEY
    assert int(rows[-1]["run_count"]) == table["unassigned"]["run_count"]


def test_partial_and_unknown_coverage_are_reported_and_exported(
    client, db_session: Session, test_user
) -> None:
    """Runs without a cost stay visible instead of being read as free."""
    seeded = _seed(db_session, test_user.account_id)
    priced, _, unassigned_run = seeded["executions"]
    issue_cost_rollup.refresh_execution_cost(
        db_session, execution_id=priced.id, estimated_cost=None
    )
    issue_cost_rollup.refresh_execution_cost(
        db_session, execution_id=unassigned_run.id, estimated_cost=None
    )
    db_session.commit()

    body = client.get(BASE).json()

    row = body["issues"][0]
    # The legacy subtotal keeps its value and meaning: the priced runs only.
    assert row["estimated_cost"] == 0.25
    assert row["cost_coverage"] == "partial"
    assert (row["known_cost_run_count"], row["unknown_cost_run_count"]) == (1, 1)
    assert row["attributed_cost_usd"] is None
    assert body["by_project"][0]["cost_coverage"] == "partial"
    assert body["by_project"][0]["attributed_cost_usd"] is None
    assert body["by_flow"][0]["cost_coverage"] == "partial"
    assert body["by_flow"][0]["attributed_cost_usd"] is None
    assert body["unassigned"]["cost_coverage"] == "unknown"
    assert body["unassigned"]["known_cost_run_count"] == 0
    assert body["unassigned"]["unknown_cost_run_count"] == 1
    assert body["unassigned"]["estimated_cost"] == 0.0
    assert body["unassigned"]["attributed_cost_usd"] is None

    exported = client.get(f"{BASE}/export", params={"format": "json"}).json()

    exported_row = exported["issues"][0]
    assert exported_row["cost_coverage"] == "partial"
    assert exported_row["known_cost_run_count"] == 1
    assert exported_row["unknown_cost_run_count"] == 1
    assert exported_row["attributed_cost_usd"] is None
    assert exported["unassigned"]["cost_coverage"] == "unknown"

    csv_response = client.get(f"{BASE}/export", params={"format": "csv"})

    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert rows[0]["cost_coverage"] == "partial"
    assert rows[0]["known_cost_run_count"] == "1"
    assert rows[0]["unknown_cost_run_count"] == "1"
    # A nullable attributed cost is an empty cell in CSV, never 0.
    assert rows[0]["attributed_cost_usd"] == ""
    assert float(rows[0]["estimated_cost"]) == 0.25
    assert rows[-1]["cost_coverage"] == "unknown"
    assert rows[-1]["attributed_cost_usd"] == ""


def test_json_export_carries_execution_ids(
    client, db_session: Session, test_user
) -> None:
    seeded = _seed(db_session, test_user.account_id)

    response = client.get(f"{BASE}/export", params={"format": "json"})

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["issues"][0]["execution_ids"]) == sorted(
        str(execution.id) for execution in seeded["executions"][:2]
    )
    assert body["unassigned"]["execution_ids"] == [str(seeded["executions"][2].id)]
    assert client.get(f"{BASE}/export", params={"format": "xml"}).status_code == 422


def test_rebuild_records_unrecorded_history(
    client, db_session: Session, test_user
) -> None:
    _seed(db_session, test_user.account_id, record=False)
    assert client.get(BASE).json()["issues"] == []
    window = {
        "start_date": (T0 - timedelta(days=1)).isoformat(),
        "end_date": (T0 + timedelta(days=1)).isoformat(),
    }

    response = client.post(f"{BASE}/rebuild", json=window)

    assert response.status_code == 200
    assert response.json() == {"recorded": 3, "failed": 0, "limit_reached": False}
    assert client.get(BASE).json()["issues"][0]["run_count"] == 2
    again = client.post(f"{BASE}/rebuild", json=window)
    assert again.json()["recorded"] == 0


def test_rebuild_rejects_long_windows(client, test_user) -> None:
    response = client.post(
        f"{BASE}/rebuild",
        json={
            "start_date": T0.isoformat(),
            "end_date": (T0 + timedelta(days=200)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_rebuild_window_limit_counts_partial_days(client, test_user) -> None:
    just_over = client.post(
        f"{BASE}/rebuild",
        json={
            "start_date": T0.isoformat(),
            "end_date": (T0 + timedelta(days=92, hours=23)).isoformat(),
        },
    )
    assert just_over.status_code == 422
    exact = client.post(
        f"{BASE}/rebuild",
        json={
            "start_date": T0.isoformat(),
            "end_date": (T0 + timedelta(days=92)).isoformat(),
        },
    )
    assert exact.status_code == 200
