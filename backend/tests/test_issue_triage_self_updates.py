"""Trusted receipt and automatic-trigger regressions without provider or DB I/O."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.models import models
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.issue import CRUDIssue, issue_compliance_result
from preloop.schemas.issue_triage import provider_revision
from preloop.services.flow_trigger_service import FlowTriggerService


BODY = "Human scope.\n<!-- preloop-triage -->\nBounded acceptance."
LABELS = ["P1", "complexity:low"]
UPDATED_AT = "2026-09-13T12:00:00Z"


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Provide one complete webhook and an account-scoped trusted receipt."""
    revision = provider_revision("Issue", BODY, LABELS, "open")
    issue = SimpleNamespace(
        tracker_id="tracker",
        project_id="project",
        meta_data={
            "preloop_triage": {
                "provider_revision": revision,
                "provider_updated_at": UPDATED_AT,
            }
        },
    )
    lookup = MagicMock(return_value=issue)
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.crud_issue.get_by_external_url",
        lookup,
    )
    flow = SimpleNamespace(
        id="triage-flow",
        name="Triage",
        is_enabled=True,
        trigger_config=None,
        allowed_mcp_tools=[{"name": "apply_issue_triage"}],
    )
    event = {
        "type": "issue_updated",
        "source": "github",
        "account_id": "account",
        "tracker_id": "tracker",
        "project_id": "project",
        "payload": {
            "action": "edited",
            "changes": {"body": {"from": "Human scope."}},
            "sender": {"login": "human-pat-owner"},
            "issue": {
                "title": "Issue",
                "updated_at": UPDATED_AT,
                "body": BODY,
                "state": "open",
                "html_url": "https://github.com/example/project/issues/17",
                "labels": [{"name": label} for label in LABELS],
            },
        },
    }
    return SimpleNamespace(
        service=FlowTriggerService(MagicMock()),
        issue=issue,
        event=event,
        flow=flow,
        lookup=lookup,
        revision=revision,
    )


def test_exact_verified_pat_update_is_suppressed(guard: SimpleNamespace) -> None:
    assert not guard.service._is_preloop_triggered_event(guard.event)
    assert guard.service._is_triage_self_update(guard.flow, guard.event)
    assert guard.lookup.call_args.kwargs == {
        "external_url": "https://github.com/example/project/issues/17",
        "account_id": "account",
    }


def test_prewrite_intent_covers_early_webhook(guard: SimpleNamespace) -> None:
    guard.issue.meta_data["preloop_triage"] = {
        "expected_revisions": [guard.revision],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    assert guard.service._is_triage_self_update(guard.flow, guard.event)


def test_gitlab_complete_snapshot_is_suppressed(guard: SimpleNamespace) -> None:
    event = guard.event
    event["source"] = "gitlab"
    event["payload"] = {
        "changes": {"description": {"previous": "Human scope.", "current": BODY}},
        "object_kind": "issue",
        "user": {"username": "human-pat-owner"},
        "object_attributes": {
            "action": "update",
            "updated_at": "2026-09-13 12:00:00 UTC",
            "title": "Issue",
            "description": BODY,
            "state": "opened",
            "url": "https://gitlab.example/group/project/-/issues/17",
        },
        "labels": [{"title": label} for label in LABELS],
    }
    guard.issue.meta_data["preloop_triage"]["provider_revision"] = provider_revision(
        "Issue", BODY, LABELS, "open"
    )
    assert guard.service._is_triage_self_update(guard.flow, event)


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Human changed title"),
        ("body", BODY + "\nHuman acceptance"),
        ("labels", [{"name": "P1"}, {"name": "complexity:high"}]),
        ("state", "closed"),
    ],
)
def test_genuine_human_changes_remain_eligible(
    guard: SimpleNamespace, field: str, value: Any
) -> None:
    guard.event["payload"]["issue"][field] = value
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.parametrize(
    "action", ["assigned", "unassigned", "reopened", "closed", "milestoned"]
)
def test_non_content_actions_remain_eligible(
    guard: SimpleNamespace, action: str
) -> None:
    guard.event["payload"]["action"] = action
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)
    guard.lookup.assert_not_called()


@pytest.mark.parametrize("changed_field", ["assignees", "milestone", "state"])
def test_gitlab_non_written_field_changes_remain_eligible(
    guard: SimpleNamespace, changed_field: str
) -> None:
    guard.event["source"] = "gitlab"
    guard.event["payload"] = {
        "object_kind": "issue",
        "changes": {changed_field: {"previous": None}},
        "object_attributes": {"action": "update"},
    }
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)
    guard.lookup.assert_not_called()


def test_later_identical_content_update_is_not_a_final_receipt_match(
    guard: SimpleNamespace,
) -> None:
    guard.issue.meta_data["preloop_triage"].update(
        {
            "expected_revisions": [guard.revision],
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        }
    )
    guard.event["payload"]["issue"]["updated_at"] = "2026-09-13T13:00:00Z"
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


def test_missing_final_timestamp_does_not_suppress_indefinitely(
    guard: SimpleNamespace,
) -> None:
    guard.issue.meta_data["preloop_triage"].pop("provider_updated_at")
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.parametrize("event_type", ["manual", "issue_opened", "issue_labeled"])
def test_other_event_types_remain_eligible(
    guard: SimpleNamespace, event_type: str
) -> None:
    guard.event["type"] = event_type
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)
    guard.lookup.assert_not_called()


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "update_issue"}],
        [{"name": "apply_issue_triage", "mcp_server_id": "external-server"}],
    ],
)
def test_unrelated_flows_remain_eligible(guard: SimpleNamespace, tools: list) -> None:
    guard.flow.allowed_mcp_tools = tools
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)
    guard.lookup.assert_not_called()


@pytest.mark.parametrize("field", ["body", "title", "labels", "state"])
def test_incomplete_snapshot_cannot_suppress(
    guard: SimpleNamespace, field: str
) -> None:
    del guard.event["payload"]["issue"][field]
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.parametrize("receipt", [None, {}, {"human_revision": "unchanged"}])
def test_marker_without_trusted_exact_receipt_cannot_suppress(
    guard: SimpleNamespace, receipt: dict | None
) -> None:
    guard.issue.meta_data = {"preloop_triage": receipt}
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.parametrize("field", ["tracker_id", "project_id"])
def test_other_resource_identity_cannot_suppress(
    guard: SimpleNamespace, field: str
) -> None:
    setattr(guard.issue, field, "different-resource")
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.parametrize(
    "expiry", ["invalid", "2099-01-01T00:00:00", "2000-01-01T00:00:00Z"]
)
def test_invalid_or_expired_intent_cannot_suppress(
    guard: SimpleNamespace, expiry: str
) -> None:
    guard.issue.meta_data["preloop_triage"] = {
        "expected_revisions": [guard.revision],
        "expires_at": expiry,
    }
    assert not guard.service._is_triage_self_update(guard.flow, guard.event)


@pytest.mark.asyncio
async def test_process_event_skips_verified_pat_update_before_dispatch(
    guard: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("preloop.services.flow_feedback.ingest_feedback", MagicMock())
    monkeypatch.setattr(
        "preloop.services.flow_feedback.feedback_policy", lambda _: None
    )
    monkeypatch.setattr(guard.service, "_extract_project_id", lambda _: "project")
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
        MagicMock(return_value=[guard.flow]),
    )
    dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.get_nats_client", dispatch
    )

    await guard.service.process_event(guard.event)

    guard.lookup.assert_called_once()
    dispatch.assert_not_awaited()


@pytest.fixture
def crud_capture(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Exercise CRUD sanitization while replacing persistence with memory."""
    updates: list[dict] = []

    def update(self: Any, db: Any, *, db_obj: Any, obj_in: dict) -> Any:
        updates.append(deepcopy(obj_in))
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    def create(self: Any, db: Any, *, obj_in: dict, commit: bool = True) -> Any:
        return SimpleNamespace(**obj_in)

    monkeypatch.setattr(CRUDBase, "update", update)
    monkeypatch.setattr(CRUDBase, "create", create)
    monkeypatch.setattr(issue_compliance_result, "delete_by_issue_id", MagicMock())
    return SimpleNamespace(
        crud=CRUDIssue(models.Issue), updates=updates, db=MagicMock()
    )


def test_ingestion_cannot_forge_receipt_on_create(
    crud_capture: SimpleNamespace,
) -> None:
    incoming = {"meta_data": {"labels": ["P1"], "preloop_triage": {"fake": True}}}
    result = crud_capture.crud.create(crud_capture.db, obj_in=incoming)
    assert result.meta_data == {"labels": ["P1"]}
    assert "preloop_triage" in incoming["meta_data"]


@pytest.mark.parametrize(
    "incoming", [None, {"labels": ["new"]}, {"preloop_triage": {"fake": True}}]
)
def test_ingestion_preserves_trusted_receipt(
    crud_capture: SimpleNamespace, incoming: dict | None
) -> None:
    receipt = {"provider_revision": "trusted"}
    issue = SimpleNamespace(id="issue", meta_data={"preloop_triage": receipt})
    result = crud_capture.crud.update(
        crud_capture.db, db_obj=issue, obj_in={"meta_data": incoming}
    )
    assert result.meta_data["preloop_triage"] == receipt


def test_ingestion_cannot_introduce_receipt_on_update(
    crud_capture: SimpleNamespace,
) -> None:
    issue = SimpleNamespace(id="issue", meta_data={})
    result = crud_capture.crud.update(
        crud_capture.db,
        db_obj=issue,
        obj_in={"meta_data": {"labels": ["P1"], "preloop_triage": {"fake": True}}},
    )
    assert result.meta_data == {"labels": ["P1"]}


def test_trusted_setter_can_replace_and_clear_receipt(
    crud_capture: SimpleNamespace,
) -> None:
    issue = SimpleNamespace(id="issue", meta_data={"labels": ["human-label"]})
    crud_capture.crud.set_triage_receipt(
        crud_capture.db, db_obj=issue, receipt={"expected_revisions": ["pending"]}
    )
    assert issue.meta_data == {
        "labels": ["human-label"],
        "preloop_triage": {"expected_revisions": ["pending"]},
    }
    crud_capture.crud.set_triage_receipt(crud_capture.db, db_obj=issue, receipt=None)
    assert issue.meta_data == {"labels": ["human-label"]}


def test_stale_webhook_session_refreshes_receipt_before_merging(
    crud_capture: SimpleNamespace,
) -> None:
    issue = SimpleNamespace(id="issue", meta_data={"labels": ["old"]})

    def refresh(db_obj: Any, **kwargs: Any) -> None:
        assert kwargs == {"attribute_names": ["meta_data"], "with_for_update": True}
        db_obj.meta_data = {"preloop_triage": {"provider_revision": "just-committed"}}

    crud_capture.db.refresh.side_effect = refresh
    result = crud_capture.crud.update(
        crud_capture.db, db_obj=issue, obj_in={"meta_data": {"labels": ["new"]}}
    )

    assert result.meta_data == {
        "labels": ["new"],
        "preloop_triage": {"provider_revision": "just-committed"},
    }
    crud_capture.db.refresh.assert_called_once()


def test_trusted_setter_refreshes_concurrent_human_metadata(
    crud_capture: SimpleNamespace,
) -> None:
    issue = SimpleNamespace(id="issue", meta_data={"labels": ["old"]})

    def refresh(db_obj: Any, **kwargs: Any) -> None:
        assert kwargs == {"attribute_names": ["meta_data"], "with_for_update": True}
        db_obj.meta_data = {"labels": ["human-added"]}

    crud_capture.db.refresh.side_effect = refresh
    result = crud_capture.crud.set_triage_receipt(
        crud_capture.db, db_obj=issue, receipt={"expected_revisions": ["pending"]}
    )

    assert result.meta_data == {
        "labels": ["human-added"],
        "preloop_triage": {"expected_revisions": ["pending"]},
    }
