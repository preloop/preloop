"""The webhook worker records approval and merge times for the issue rollup."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

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
