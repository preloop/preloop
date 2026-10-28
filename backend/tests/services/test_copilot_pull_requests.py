"""Copilot coding agent pull requests in the pull request record (#2018)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import webhooks
from preloop.models import models
from preloop.models.crud import crud_issue_cost
from preloop.services import issue_cost_rollup as rollup_service
from preloop.services import pull_request_refresh as refresh
from preloop.services.copilot_pull_requests import (
    copilot_author_id,
    record_copilot_pull_request,
)
from tests.models.crud.test_forge_reconciliation import FakeForge
from tests.services.test_issue_cost_rollup import World

FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "copilot" / "pull_request_opened.json"
)
PR_KEY = "https://github.com/example-org/example-repo/pull/41"


@pytest.fixture
def world(db_session: Session) -> World:
    return World(db_session, name="copilot")


def _event(world: World, **pull_overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(json.loads(FIXTURE.read_text()))
    payload["pull_request"].update(pull_overrides)
    return {
        "source": "github",
        "tracker_id": str(world.tracker.id),
        "account_id": str(world.account.id),
        "type": "pull_request_opened",
        "payload": payload,
    }


def _links(world: World, row: Any) -> list[Any]:
    return crud_issue_cost.issue_ids_for_pull_request(
        world.db, account_id=world.account.id, pull_request_id=row.id
    )


def test_recorded_copilot_pr_gets_origin_author_and_issue_link(world: World) -> None:
    rollup_service.record_pull_request_event_safely(world.db, _event(world))

    row = crud_issue_cost.get_pull_request(
        world.db, account_id=world.account.id, pr_key=PR_KEY
    )
    assert row is not None
    assert row.origin == "copilot"
    assert row.author_id == "198982749"
    assert row.state == "open"
    assert row.head_sha == "0123456789abcdef0123456789abcdef01234567"
    assert row.opened_at == datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert _links(world, row) == [world.issue.id]


def test_display_name_spoof_is_rejected(world: World) -> None:
    spoof = {"login": "Copilot", "id": 4242, "type": "User", "name": "Copilot"}
    event = _event(world, user=spoof)
    assert copilot_author_id(event) is None

    rollup_service.record_pull_request_event_safely(world.db, event)

    assert (
        crud_issue_cost.get_pull_request(
            world.db, account_id=world.account.id, pr_key=PR_KEY
        )
        is None
    )


def test_agent_id_on_a_user_account_is_not_trusted(world: World) -> None:
    event = _event(world, user={"login": "someone", "id": 198982749, "type": "User"})
    assert copilot_author_id(event) is None


def test_branch_name_links_when_no_closing_keyword(world: World) -> None:
    event = _event(
        world, body="Work on the chart legend.", head={"ref": "copilot/fix-13"}
    )
    row = record_copilot_pull_request(world.db, event)
    world.db.commit()

    assert row is not None
    assert _links(world, row) == [world.other_issue.id]


def test_closing_keyword_wins_over_branch(world: World) -> None:
    event = _event(world, body="Closes #12", head={"ref": "copilot/fix-13"})
    row = record_copilot_pull_request(world.db, event)
    world.db.commit()

    assert row is not None
    assert _links(world, row) == [world.issue.id]


def test_unsynced_issue_is_not_linked(world: World) -> None:
    row = record_copilot_pull_request(world.db, _event(world, body="Fixes #99"))
    world.db.commit()

    assert row is not None
    assert row.origin == "copilot"
    assert _links(world, row) == []


def test_replay_is_idempotent_and_merge_is_final(world: World) -> None:
    merged = _event(world, state="closed", merged=True)
    record_copilot_pull_request(world.db, merged)
    record_copilot_pull_request(world.db, _event(world))
    world.db.commit()

    row = crud_issue_cost.get_pull_request(
        world.db, account_id=world.account.id, pr_key=PR_KEY
    )
    assert row is not None
    assert row.state == "merged"
    assert _links(world, row) == [world.issue.id]


def test_other_forges_are_ignored(world: World) -> None:
    event = _event(world)
    event["source"] = "gitlab"
    assert copilot_author_id(event) is None


@pytest.mark.asyncio
async def test_copilot_webhooks_share_dedupe_worker_and_refresh_bound(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import flow_trigger_service
    from preloop.sync import tasks

    # Keep the real ingress claim and worker path, replacing HTTP preparation
    # and the queue transport only. The worker must create the Copilot records.
    def prepare(
        tracker_type: str,
        organization_id: str,
        raw_body: bytes,
        headers: Any,
        db: Any,
        plan: Any,
    ) -> dict[str, Any]:
        plan.tracker_id = world.tracker.id
        plan.queue_task(
            "process_webhook_event",
            tracker_id=world.tracker.id,
            event_type="pull_request",
            payload=json.loads(raw_body),
            delivery_id=headers["X-GitHub-Delivery"],
        )
        return {"status": "success"}

    async def inline(fn: Any) -> Any:
        return fn()

    queued: list[dict[str, Any]] = []

    async def publish(name: str, **kwargs: Any) -> object:
        assert name == "process_webhook_event"
        queued.append(kwargs)
        return object()

    monkeypatch.setattr(webhooks, "_prepare_webhook", prepare)
    monkeypatch.setattr(webhooks, "run_db_off_loop", inline)
    monkeypatch.setattr(webhooks, "_safe_close_db_session", lambda db: None)
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([world.db]))
    monkeypatch.setattr(world.db, "close", lambda: None)
    monkeypatch.setattr(
        flow_trigger_service.crud_flow, "get_by_trigger", lambda *a, **k: []
    )
    for number in range(1, 51):
        event = _event(
            world, number=number, html_url=PR_KEY.rsplit("/", 1)[0] + f"/{number}"
        )
        payload = event["payload"]
        payload["number"] = number
        request = MagicMock(
            body=AsyncMock(return_value=json.dumps(payload).encode()),
            headers={"X-GitHub-Delivery": f"copilot-{number}"},
        )
        bus = MagicMock(publish_task=publish)
        await webhooks.receive_webhook("github", "org", request, world.db, bus)
        duplicate = await webhooks.receive_webhook(
            "github", "org", request, world.db, bus
        )
        assert duplicate["status"] == "duplicate"
    assert len(queued) == 50
    for task in queued:
        await tasks.process_webhook_event(**task)
    rows = (
        world.db.query(models.IssueCostPullRequest)
        .filter_by(account_id=world.account.id)
        .all()
    )
    assert len(rows) == 50
    assert {row.origin for row in rows} == {"copilot"}
    assert all(_links(world, row) == [world.issue.id] for row in rows)
    forge = FakeForge()
    monkeypatch.setattr(
        refresh, "feedback_tracker_client", AsyncMock(return_value=forge)
    )
    now = datetime.now(UTC)
    counts = [
        await refresh.refresh_pull_requests(
            world.db, now=now + timedelta(minutes=minute)
        )
        for minute in range(3)
    ]
    assert [count["read"] for count in counts] == [25, 25, 0]
    assert len({path for path, _ in forge.reads}) == 50
    # Queue replay is also idempotent; it cannot add PR rows or issue links.
    await tasks.process_webhook_event(**queued[0])
    assert (
        world.db.query(models.IssueCostPullRequest)
        .filter_by(account_id=world.account.id)
        .count()
        == 50
    )
