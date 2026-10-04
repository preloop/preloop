"""The webhook worker records approval and merge times for the issue rollup."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from preloop.models import models
from preloop.services import issue_cost_rollup
from preloop.sync import tasks


@pytest.mark.asyncio
async def test_issue_cost_hook_runs_after_triggers_and_before_ack(
    monkeypatch: Any,
) -> None:
    order: list[str] = []
    recorded: list[dict[str, Any]] = []

    async def process(event: dict[str, Any]) -> None:
        order.append("flow")

    async def ack() -> None:
        order.append("ack")

    def record(db: Any, event_data: dict[str, Any]) -> None:
        order.append("rollup")
        recorded.append(event_data)

    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        tasks.crud_tracker,
        "get",
        MagicMock(
            return_value=SimpleNamespace(
                id="tracker", tracker_type="github", account_id="account"
            )
        ),
    )
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        lambda db: SimpleNamespace(process_event=process),
    )
    monkeypatch.setattr(
        "preloop.services.issue_cost_rollup.record_pull_request_event_safely", record
    )

    await tasks.process_webhook_event(
        "tracker",
        "pull_request",
        {"action": "closed", "pull_request": {"merged": True}},
        _ack=ack,
    )

    assert order == ["flow", "rollup", "ack"]
    assert recorded[0]["type"] == "pull_request_merged"
    assert recorded[0]["account_id"] == "account"


def test_non_review_events_skip_the_rollup_transaction() -> None:
    from preloop.services.issue_cost_rollup import record_pull_request_event_safely

    db = MagicMock()
    record_pull_request_event_safely(db, {"type": "issue_opened"})
    db.begin_nested.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_bitbucket_approval_and_merge_reach_the_issue_row(
    monkeypatch: Any, db_session: Any, test_user: Any
) -> None:
    """Raw Bitbucket Cloud deliveries go through the worker to the rollup (#1064).

    Before Bitbucket payloads were parsed, the worker passed these events on
    and the rollup dropped them, so a Jira ticket with a Bitbucket pull
    request never got approval or merge times.
    """
    from tests import issue_cost_reconciliation as rec

    fixture = rec.seed_reconciliation(db_session, test_user.account_id)
    # Start from a pull request without approval and merge times.
    pull = db_session.scalars(
        select(models.IssueCostPullRequest).where(
            models.IssueCostPullRequest.account_id == test_user.account_id,
            models.IssueCostPullRequest.pr_key == rec.PR_URL,
        )
    ).one()
    pull.approved_at = None
    pull.merged_at = None
    db_session.commit()

    async def process(event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db_session]))
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        lambda db: SimpleNamespace(process_event=process),
    )
    base = {
        "actor": {"nickname": "reviewer"},
        "repository": {"full_name": rec.WORKSPACE_REPO},
    }
    approved_at = rec.APPROVED.isoformat()
    for event_key, extra, pull_payload in (
        (
            "pullrequest:approved",
            {"approval": {"date": approved_at}},
            rec.bitbucket_pull_request(updated=rec.APPROVED),
        ),
        (
            "pullrequest:fulfilled",
            {},
            rec.bitbucket_pull_request(state="MERGED", updated=rec.MERGED),
        ),
    ):
        await tasks.process_webhook_event(
            str(fixture.bitbucket_tracker.id),
            event_key,
            {**base, **extra, "pullrequest": pull_payload},
        )

    row = next(
        row
        for row in issue_cost_rollup.build_report(
            db_session, account_id=test_user.account_id
        ).issues
        if row.issue_key == rec.JIRA_KEY
    )
    assert row.approved_at == rec.APPROVED
    assert row.merged_at == rec.MERGED
    assert row.pr_opened_to_approved_hours == 2.0
    assert row.approved_to_merged_hours == 1.0
    assert row.run_count == 5
