"""Jira label and status triggers derived from the issue update changelog.

Jira sends every edit as ``jira:issue_updated``. The changelog items say
what changed, so a flow can fire when a label is added or when the issue
moves to a status, instead of on every edit.
"""

import copy
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.event_normalizer import (
    EVENT_TYPE_LABELS,
    extract_filter_fields,
    humanize_event_type,
    jira_label_delta,
    jira_status_change,
    normalize_event_type,
    secondary_event_types,
)


def jira_updated(
    items: List[Dict[str, Any]],
    *,
    labels: Optional[List[str]] = None,
    status: str = "To Do",
) -> Dict[str, Any]:
    """A ``jira:issue_updated`` delivery with the given changelog items."""
    return {
        "timestamp": 1790000000000,
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_generic",
        "user": {"accountId": "acc-1", "displayName": "Example User"},
        "issue": {
            "id": "10001",
            "key": "PROJ-12",
            "fields": {
                "summary": "Example issue",
                "labels": labels or [],
                "status": {"name": status},
                "project": {"id": "10000", "key": "PROJ"},
            },
        },
        "changelog": {"id": "20001", "items": items},
    }


def labels_item(before: str, after: str) -> Dict[str, Any]:
    return {
        "field": "labels",
        "fieldtype": "jira",
        "from": None,
        "fromString": before,
        "to": None,
        "toString": after,
    }


def status_item(before: str, after: str) -> Dict[str, Any]:
    return {
        "field": "status",
        "fieldtype": "jira",
        "from": "10000",
        "fromString": before,
        "to": "10001",
        "toString": after,
    }


def summary_item() -> Dict[str, Any]:
    return {
        "field": "summary",
        "fieldtype": "jira",
        "fromString": "Old title",
        "toString": "Example issue",
    }


class TestJiraLabelDelta:
    def test_added_label(self) -> None:
        payload = jira_updated([labels_item("backend", "agent-ready backend")])
        assert jira_label_delta(payload) == (["agent-ready"], [])

    def test_removed_label(self) -> None:
        payload = jira_updated([labels_item("agent-ready backend", "backend")])
        assert jira_label_delta(payload) == ([], ["agent-ready"])

    def test_first_label_from_empty(self) -> None:
        payload = jira_updated([labels_item("", "agent-ready")])
        assert jira_label_delta(payload) == (["agent-ready"], [])

    def test_null_strings_are_empty(self) -> None:
        item = labels_item("", "")
        item["fromString"] = None
        item["toString"] = "agent-ready"
        assert jira_label_delta(jira_updated([item])) == (["agent-ready"], [])

    def test_no_changelog(self) -> None:
        payload = jira_updated([])
        del payload["changelog"]
        assert jira_label_delta(payload) == ([], [])
        assert jira_label_delta(None) == ([], [])

    def test_malformed_changelog_is_ignored(self) -> None:
        payload = jira_updated([])
        payload["changelog"] = {"items": "not-a-list"}
        assert jira_label_delta(payload) == ([], [])
        assert jira_status_change(payload) is None


class TestJiraStatusChange:
    def test_transition(self) -> None:
        payload = jira_updated([status_item("To Do", "In Progress")])
        assert jira_status_change(payload) == ("To Do", "In Progress")

    def test_no_status_item(self) -> None:
        assert jira_status_change(jira_updated([summary_item()])) is None

    def test_same_status_is_not_a_transition(self) -> None:
        assert jira_status_change(jira_updated([status_item("Done", "Done")])) is None


class TestJiraNormalization:
    def test_label_added_is_issue_labeled(self) -> None:
        payload = jira_updated([labels_item("", "agent-ready")])
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_labeled"
        )

    def test_label_removed_is_issue_unlabeled(self) -> None:
        payload = jira_updated([labels_item("agent-ready", "")])
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_unlabeled"
        )

    def test_transition_is_issue_status_changed(self) -> None:
        payload = jira_updated([status_item("To Do", "Ready for Dev")])
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_status_changed"
        )

    def test_other_edit_stays_issue_updated(self) -> None:
        payload = jira_updated([summary_item()])
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_updated"
        )

    def test_without_payload_stays_issue_updated(self) -> None:
        assert normalize_event_type("jira", "jira:issue_updated") == "issue_updated"

    def test_added_label_wins_over_transition(self) -> None:
        """One edit, one event type: an added label takes precedence."""
        payload = jira_updated(
            [status_item("To Do", "In Progress"), labels_item("", "agent-ready")]
        )
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_labeled"
        )

    def test_transition_wins_over_removed_label(self) -> None:
        payload = jira_updated(
            [labels_item("agent-ready", ""), status_item("To Do", "In Progress")]
        )
        assert (
            normalize_event_type("jira", "jira:issue_updated", payload)
            == "issue_status_changed"
        )

    def test_created_is_unaffected_by_changelog_shape(self) -> None:
        payload = jira_updated([labels_item("", "agent-ready")])
        payload["webhookEvent"] = "jira:issue_created"
        assert (
            normalize_event_type("jira", "jira:issue_created", payload)
            == "issue_opened"
        )

    def test_status_changed_has_a_label(self) -> None:
        assert EVENT_TYPE_LABELS["issue_status_changed"] == "Issue Status Changed"
        assert humanize_event_type("issue_status_changed") == "Issue Status Changed"


class TestJiraFilterFields:
    def test_mixed_edit_exposes_every_delta(self) -> None:
        payload = jira_updated(
            [
                labels_item("old backend", "agent-ready backend"),
                status_item("To Do", "In Progress"),
            ],
            labels=["agent-ready", "backend"],
            status="In Progress",
        )
        fields = extract_filter_fields("jira", "jira:issue_updated", payload)
        assert fields["added_labels"] == ["agent-ready"]
        assert fields["removed_labels"] == ["old"]
        assert fields["status_from"] == "To Do"
        assert fields["status_to"] == "In Progress"
        assert fields["state"] == "In Progress"
        assert fields["labels"] == ["agent-ready", "backend"]

    def test_plain_edit_has_no_deltas(self) -> None:
        fields = extract_filter_fields(
            "jira", "jira:issue_updated", jira_updated([summary_item()])
        )
        for key in ("added_labels", "removed_labels", "status_from", "status_to"):
            assert key not in fields


@pytest.fixture
def service() -> FlowTriggerService:
    db = MagicMock(spec=Session)
    return FlowTriggerService(db, MagicMock())


def enriched_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror ``process_webhook_event``: normalize, then merge filter fields."""
    payload = copy.deepcopy(payload)
    event_type = normalize_event_type("jira", "jira:issue_updated", payload)
    fields = extract_filter_fields("jira", "jira:issue_updated", payload)
    return {
        "source": "jira",
        "type": event_type,
        "account_id": str(uuid.uuid4()),
        "payload": {**payload, **fields},
    }


def flow_with(trigger_config: Dict[str, Any]) -> MagicMock:
    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.name = "Jira flow"
    flow.trigger_config = trigger_config
    return flow


class TestJiraTriggerMatching:
    def test_label_flow_fires_only_on_the_added_label(self, service) -> None:
        flow = flow_with({"filter_conditions": {"labels": ["agent-ready"]}})
        configured = enriched_event(
            jira_updated(
                [labels_item("backend", "agent-ready backend")],
                labels=["agent-ready", "backend"],
            )
        )
        unrelated = enriched_event(
            jira_updated(
                [labels_item("agent-ready", "agent-ready backend")],
                labels=["agent-ready", "backend"],
            )
        )
        assert configured["type"] == "issue_labeled"
        assert service._matches_trigger_config(flow, configured) is True
        assert unrelated["type"] == "issue_labeled"
        assert service._matches_trigger_config(flow, unrelated) is False

    def test_status_flow_fires_on_the_target_status(self, service) -> None:
        flow = flow_with({"filter_conditions": {"status_to": "Ready for Dev"}})
        moved = enriched_event(jira_updated([status_item("To Do", "Ready for Dev")]))
        elsewhere = enriched_event(jira_updated([status_item("To Do", "Done")]))
        assert moved["type"] == "issue_status_changed"
        assert service._matches_trigger_config(flow, moved) is True
        assert service._matches_trigger_config(flow, elsewhere) is False

    def test_status_flow_ignores_edits_without_a_transition(self, service) -> None:
        flow = flow_with({"filter_conditions": {"status_to": "Ready for Dev"}})
        edit = enriched_event(jira_updated([summary_item()], status="Ready for Dev"))
        assert service._matches_trigger_config(flow, edit) is False


class TestMixedEditReachesStatusFlows:
    def test_secondary_type_for_a_mixed_edit(self) -> None:
        event = enriched_event(
            jira_updated(
                [status_item("To Do", "In Progress"), labels_item("", "agent-ready")]
            )
        )
        assert event["type"] == "issue_labeled"
        assert secondary_event_types("jira", event["type"], event["payload"]) == (
            "issue_status_changed",
        )

    def test_no_secondary_type_otherwise(self) -> None:
        labeled = enriched_event(jira_updated([labels_item("", "agent-ready")]))
        moved = enriched_event(jira_updated([status_item("To Do", "Done")]))
        assert secondary_event_types("jira", labeled["type"], labeled["payload"]) == ()
        assert secondary_event_types("jira", moved["type"], moved["payload"]) == ()
        assert (
            secondary_event_types("github", "issue_labeled", {"status_to": "x"}) == ()
        )
        assert secondary_event_types("jira", "issue_labeled", None) == ()

    def test_status_flows_are_added_once(self, service) -> None:
        event = enriched_event(
            jira_updated(
                [status_item("To Do", "In Progress"), labels_item("", "agent-ready")]
            )
        )
        label_flow = flow_with({"labels": ["agent-ready"]})
        status_flow = flow_with({"status_to": "In Progress"})
        with patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=[label_flow, status_flow],
        ) as lookup:
            flows = service._add_secondary_event_flows(
                event,
                [label_flow],
                query_source="tracker-1",
                project_id=None,
                account_id=event["account_id"],
            )
        assert flows == [label_flow, status_flow]
        assert lookup.call_args.kwargs["event_type"] == "issue_status_changed"
        assert service._matches_trigger_config(status_flow, event) is True

    def test_plain_events_do_not_query_again(self, service) -> None:
        event = enriched_event(jira_updated([labels_item("", "agent-ready")]))
        with patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger"
        ) as lookup:
            flows = service._add_secondary_event_flows(
                event, [], query_source="t", project_id=None, account_id=None
            )
        assert flows == []
        lookup.assert_not_called()
