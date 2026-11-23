"""Azure Boards work item events in FlowTriggerService and the scope resolver.

Deliveries are Microsoft's service hook samples from
``tests/fixtures/azure_devops/work_items.json``, normalized by the intake
helper exactly as the webhook endpoint does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.azure_work_item_scope import resolve_work_item_project
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.utils.azure_devops_work_items import normalize_work_item_delivery

EVENTS: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "azure_devops"
        / "work_items.json"
    ).read_text()
)["events"]
BOT_ID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"


def envelope(
    key: str, event_type: str, *, revised_by: str | None = None
) -> Dict[str, Any]:
    raw = copy.deepcopy(EVENTS[key])
    if revised_by:
        raw["resource"]["revisedBy"]["id"] = revised_by
    payload = normalize_work_item_delivery(
        raw, organization="fabrikam", own_identity_ids=[BOT_ID]
    ).payload
    return {"source": "azure_devops", "type": event_type, "payload": payload}


@pytest.fixture
def service() -> FlowTriggerService:
    return FlowTriggerService(MagicMock(spec=Session))


def test_resource_key_names_the_work_item() -> None:
    event = envelope("workitem.created", "issue_opened")
    assert FlowTriggerService._extract_resource_key(event) == (
        "azure_devops:FabrikamCloud:issue:5"
    )


def test_own_update_does_not_trigger_but_human_and_label_events_do(
    service: FlowTriggerService,
) -> None:
    own = envelope("workitem.updated", "issue_updated", revised_by=BOT_ID)
    assert service._is_preloop_triggered_event(own) is True
    # A tag Preloop adds is the hand-off onto an implementation flow: it is
    # exempt from the loop guard, as for every other tracker.
    own_label = envelope("workitem.updated", "issue_labeled", revised_by=BOT_ID)
    assert service._is_preloop_triggered_event(own_label) is False
    human = envelope("workitem.updated", "issue_updated")
    assert service._is_preloop_triggered_event(human) is False


def _project(repository: str, board_project: str = "FabrikamCloud") -> MagicMock:
    project = MagicMock()
    project.meta_data = {"repository": repository, "project": board_project}
    project.name = repository
    project.is_active = True
    return project


@pytest.mark.parametrize(
    ("repos", "details", "expected"),
    [
        (["FabrikamCloud"], {}, "FabrikamCloud"),
        (["api", "web"], {}, None),
        (["api", "FabrikamCloud"], {}, "FabrikamCloud"),
        (["api", "web"], {"work_item_repositories": {"FabrikamCloud": "web"}}, "web"),
    ],
)
def test_resolver_picks_the_home_repository(
    repos: list, details: dict, expected: Any
) -> None:
    projects = [_project(r) for r in repos] + [_project("other", "OtherProject")]
    tracker = MagicMock()
    tracker.id = "t1"
    tracker.connection_details = {"organization": "fabrikam", **details}
    with patch(
        "preloop.services.azure_work_item_scope.crud_project.get_for_tracker",
        return_value=projects,
    ):
        found = resolve_work_item_project(MagicMock(), tracker, "fabrikamcloud")
    assert (found.meta_data["repository"] if found else None) == expected
