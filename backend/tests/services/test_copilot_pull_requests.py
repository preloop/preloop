"""Copilot coding agent pull requests in the pull request record (#2018)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_issue_cost
from preloop.services import issue_cost_rollup as rollup_service
from preloop.services.copilot_pull_requests import (
    copilot_author_id,
    record_copilot_pull_request,
)
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
