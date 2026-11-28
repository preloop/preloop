"""CI health view and weekly value summary from the ledger and executions."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import flow_state as crud_flow_state
from preloop.services.ci_health import ledger as ledger_mod
from preloop.services.ci_health.value import health_view, weekly_value

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
REPO = "github:example-org/app"


def _ledger() -> dict[str, Any]:
    ledger = ledger_mod.empty(NOW - timedelta(days=20))
    for day, rate, queue in ((10, 0.8, 300.0), (8, 0.85, 240.0), (1, 0.95, 60.0)):
        ledger_mod.append_history(
            ledger,
            {
                "at": (NOW - timedelta(days=day)).isoformat(),
                "repositories": {
                    REPO: {
                        "success_rate": rate,
                        "queue_p95_s": queue,
                        "slowest_gate": "test",
                        "slowest_gate_p95_s": 900.0,
                        "runner_minutes": 400.0,
                        "rerun_rate": 0.1,
                    }
                },
            },
        )
    ledger["signatures"] = {
        "aaaaaaaaaaaa": {"test": "a", "status": "open", "flaky": True, "count": 3},
        "bbbbbbbbbbbb": {"test": "b", "status": "open", "flaky": False, "count": 9},
        "cccccccccccc": {"test": "c", "status": "regressed", "count": 1},
        "dddddddddddd": {
            "test": "d",
            "status": "fixed",
            "status_at": (NOW - timedelta(days=2)).isoformat(),
        },
        "eeeeeeeeeeee": {"test": "e", "status": "ignored", "count": 50},
    }
    ledger["pull_requests"] = [
        {
            "url": "https://github.com/example-org/app/pull/7",
            "opened_at": (NOW - timedelta(days=3)).isoformat(),
            "merged_at": (NOW - timedelta(days=2)).isoformat(),
            "state": "merged",
            "estimated_minutes_saved_per_week": 90,
        },
        {
            "url": "https://github.com/example-org/app/pull/8",
            "opened_at": (NOW - timedelta(days=1)).isoformat(),
            "state": "open",
            "estimated_minutes_saved_per_week": 500,
        },
    ]
    return ledger


def _outcomes() -> list[dict[str, Any]]:
    def row(days: float, status: str, tokens: int, cost: float) -> dict[str, Any]:
        return {
            "id": "x",
            "status": status,
            "start_time": (NOW - timedelta(days=days)).replace(tzinfo=None),
            "total_tokens": tokens,
            "estimated_cost": cost,
        }

    return [
        row(1, "SUCCEEDED", 100_000, 1.5),
        row(2, "SUCCEEDED", 300_000, 2.5),
        row(3, "FAILED", 50_000, 0.5),
        row(4, "SKIPPED", 0, 0.0),
        row(5, "STOPPED", 10_000, 0.25),
        row(9, "SUCCEEDED", 900_000, 9.0),
    ]


def test_weekly_value_counts_only_the_window() -> None:
    week = weekly_value(_ledger(), _outcomes(), start=NOW - timedelta(days=7), end=NOW)
    # A stopped run counts as failed, not as an unclassified run.
    assert week["runs"] == {"total": 4, "succeeded": 2, "failed": 2, "skipped": 1}
    assert week["tokens"] == 460_000
    assert week["cost_usd_est"] == 4.75
    assert (week["pull_requests_opened"], week["pull_requests_merged"]) == (2, 1)
    assert week["failures_fixed"] == 1
    # Only merged changes count towards claimed savings.
    assert week["runner_minutes_saved_per_week_est"] == 90
    assert week["cost_per_merged_pull_request_usd_est"] == 4.75
    assert week["tokens_per_merged_pull_request"] == 460_000


def test_health_view_trend_flakes_and_prior_week() -> None:
    view = health_view(_ledger(), _outcomes(), now=NOW, days=7)
    repo = view["repositories"][REPO]
    assert repo["latest"]["success_rate"] == 0.95
    assert repo["prior"]["success_rate"] == 0.85  # the newest point >= 7 days old
    assert [p["queue_p95_s"] for p in repo["series"]] == [300.0, 240.0, 60.0]
    assert [f["id"] for f in view["top_flakes"]] == [
        "cccccccccccc",
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
    ]
    assert view["prior_week"]["runs"]["total"] == 1
    assert view["prior_week"]["cost_per_merged_pull_request_usd_est"] is None
    # The saving is the one in force at the end of each window: the change
    # merged this week does not count in the prior week.
    assert view["week"]["runner_minutes_saved_per_week_est"] == 90
    assert view["prior_week"]["runner_minutes_saved_per_week_est"] is None
    assert view["pull_requests"][0]["url"].endswith("/pull/8")
    empty = health_view(None, [], now=NOW)
    assert empty["repositories"] == {} and empty["week"]["runs"]["total"] == 0


def test_endpoint(client: Any, db_session: Session, test_user: models.User) -> None:
    flow = models.Flow(
        name="CI health",
        account_id=test_user.account_id,
        trigger_event_source="schedule",
        prompt_template="Review CI",
        agent_type="codex",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    url = f"/api/v1/flows/{flow.id}/ci-health"
    assert client.get(url).status_code == 404
    crud_flow_state.put_state(
        db_session,
        account_id=test_user.account_id,
        flow_id=flow.id,
        key=ledger_mod.LEDGER_KEY,
        value=_ledger(),
    )
    db_session.add(
        models.FlowExecution(
            flow_id=flow.id,
            status="SUCCEEDED",
            start_time=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
            total_tokens=1000,
            estimated_cost=0.25,
        )
    )
    db_session.flush()
    response = client.get(url, params={"days": 7})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["week"]["runs"]["total"] == 1
    assert body["week"]["tokens"] == 1000
    assert REPO in body["repositories"]
    assert client.get(url, params={"days": 0}).status_code == 422
