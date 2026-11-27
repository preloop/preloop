"""The runner owner's flow approval names the device, flow and harness."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models.crud import owner_consent
from preloop.services import runner_flow_consent_context as ctx
from preloop.services.push_notifications.notification_payloads import (
    NotificationPayloadBuilder,
)


def _runner(**overrides):
    values = dict(
        id=uuid4(),
        name="Work laptop",
        hostname="laptop.example.com",
        os="darwin",
        arch="arm64",
        capabilities={
            "authorized_directories": [
                {
                    "id": "dir-1",
                    "label": "App checkout",
                    "mode": "write",
                    "harnesses": "all",
                }
            ]
        },
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_rows(monkeypatch, *, trigger, flow_overrides=None):
    flow = SimpleNamespace(
        id=uuid4(),
        name="Nightly review",
        timeout_seconds=1800,
        git_clone_config={
            "enabled": True,
            "create_pull_request": True,
            "repositories": [
                {
                    "tracker_id": str(uuid4()),
                    "repository_url": "https://example.com/acme/app.git",
                    "clone_path": "app",
                    "branch": "main",
                }
            ],
        },
        **(flow_overrides or {}),
    )
    execution = SimpleNamespace(flow_id=flow.id, trigger_event_details=trigger)
    monkeypatch.setattr(ctx.crud_flow_execution, "get", lambda *a, **k: execution)
    monkeypatch.setattr(ctx.crud_flow, "get", lambda *a, **k: flow)
    return flow


def test_context_and_summary_describe_a_scheduled_codex_run(monkeypatch) -> None:
    flow = _patch_rows(monkeypatch, trigger={"source": "schedule", "type": "schedule"})
    payload = {
        "agent_type": "codex",
        "model_identifier": "example-model",
        "host_exec": {"harness": "codex_cli", "profile": "codex"},
        "host_exec_workspace": {"kind": "authorized_directory", "id": "dir-1"},
        "timeout_seconds": 3600,
    }
    summary, rule_context = ctx.runner_flow_request(
        MagicMock(), runner=_runner(), execution_id=uuid4(), payload=payload
    )
    context = rule_context["runner_flow"]
    assert context["runner"] == {
        "name": "Work laptop",
        "device": {"hostname": "laptop.example.com", "os": "macOS", "arch": "arm64"},
    }
    assert context["harness"] == "Codex CLI"
    assert context["model"] == "example-model"
    assert context["flow"] == {
        "id": str(flow.id),
        "name": "Nightly review",
        "href": f"/console/flows/{flow.id}",
    }
    assert context["trigger"]["kind"] == "schedule"
    # A host lease carries no checkout; the flow's configuration does.
    assert context["repositories"] == [
        {"repository": "https://example.com/acme/app.git", "branch": "main"}
    ]
    assert context["directory"] == "App checkout"
    assert context["publication"]["can_push"] is True
    assert context["limits"] == {"timeout_seconds": 3600}
    assert context["scope"] == "this_run"
    assert summary == (
        'Allow flow "Nightly review" to run Codex CLI (example-model) on '
        "Work laptop (laptop.example.com, macOS)? Scheduled run."
    )


@pytest.mark.parametrize(
    ("details", "kind", "label"),
    [
        (
            {"test_mode": False, "triggered_by": "Jane Doe"},
            "manual",
            "Started manually by Jane Doe",
        ),
        (
            {"triggered_by": "Jane Doe", "retry_of_execution_id": "x"},
            "retry",
            "Retry by Jane Doe",
        ),
        (
            {"source": "github", "type": "pull_request"},
            "event",
            "github pull_request event",
        ),
        ({"parent_execution_id": "x"}, "delegated", "Started by another flow"),
        ({}, "unknown", "Unknown trigger"),
        (None, "unknown", "Unknown trigger"),
    ],
)
def test_describe_trigger(details, kind: str, label: str) -> None:
    trigger = ctx.describe_trigger(details)
    assert trigger["kind"] == kind
    assert trigger["label"] == label


def test_read_only_review_cannot_push(monkeypatch) -> None:
    _patch_rows(monkeypatch, trigger={})
    context = ctx.build_runner_flow_context(
        MagicMock(),
        runner=_runner(),
        execution_id=uuid4(),
        payload={"agent_type": "claude_code", "host_exec_read_only": True},
    )
    assert context["publication"] == {
        "can_push": False,
        "label": "Read only: cannot push or publish",
    }
    assert context["harness"] == "Claude Code"
    assert "trigger" in context and "Unknown" not in ctx.runner_flow_summary(context)


def test_failure_falls_back_to_generic_wording(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ctx.crud_flow_execution, "get", boom)
    assert ctx.runner_flow_request(
        MagicMock(), runner=_runner(), execution_id=uuid4(), payload={}
    ) == (None, None)


def test_summary_without_flow_or_harness() -> None:
    assert (
        ctx.runner_flow_summary({"runner": {"name": "ci-box"}})
        == "Allow this flow to run on ci-box?"
    )


def test_poll_of_existing_request_never_rebuilds_the_context() -> None:
    owner = uuid4()
    consent = SimpleNamespace(
        owner_user_id=owner, subject_id="runner", approval_request_id=uuid4()
    )
    approval = SimpleNamespace(
        tool_args={"runner_id": "runner"},
        execution_id="execution",
        managed_agent_id=None,
        status="pending",
        expires_at=None,
    )
    approval.expires_at = __import__("datetime").datetime.max
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = consent
    db.get.return_value = approval
    describe = MagicMock(return_value=("summary", {"runner_flow": {}}))
    with patch.object(owner_consent.crud_account_halt, "lock_account"):
        _, _, created = owner_consent.get_or_create(
            db,
            account_id=uuid4(),
            owner_user_id=owner,
            action="runner_flow",
            subject_id="runner",
            request_key="intent",
            details={"runner_id": "runner"},
            execution_id="execution",
            describe=describe,
        )
    assert not created
    describe.assert_not_called()


def test_push_payload_carries_runner_flow_context() -> None:
    context = {"runner": {"name": "Work laptop"}, "harness": "Codex CLI"}
    payload = NotificationPayloadBuilder.new_approval_request(
        request_id=str(uuid4()),
        tool_name="owner_consent",
        tool_args={"runner_id": "r", "execution_id": "e"},
        summary='Allow flow "Nightly review" to run Codex CLI on Work laptop?',
        rule_context={"runner_flow": context},
    )
    assert payload["runner_flow"] == context
    assert "Work laptop" in payload["aps"]["alert"]["body"]
