"""The orchestrator runs a backport flow without an agent (issue #961)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.backport import (
    BackportPlan,
    BackportReport,
    TargetResult,
    extract_merged_change,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

SHA = "c" * 40
CONFIG = {
    "enabled": False,
    "git_user_name": "Preloop",
    "git_user_email": "bot@example.com",
    "backport": {
        "enabled": True,
        "source_branch": "release/1.0",
        "target_branches": ["main"],
    },
}
PLAN = BackportPlan(source_branch="release/1.0", target_branches=("main",))


def event(base: str = "release/1.0") -> Dict[str, Any]:
    return {
        "source": "github",
        "tracker_id": "tracker-1",
        "type": "pull_request_merged",
        "payload": {
            "pull_request": {
                "number": 812,
                "html_url": "https://github.com/acme/widgets/pull/812",
                "title": "Fix",
                "body": "",
                "base": {"ref": base},
                "merge_commit_sha": SHA,
            },
            "repository": {"clone_url": "https://github.com/acme/widgets.git"},
        },
    }


def orchestrator(trigger: Dict[str, Any]) -> FlowExecutionOrchestrator:
    instance = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid.uuid4(),
        trigger_event_data=trigger,
        nats_client=AsyncMock(),
    )
    instance.flow = SimpleNamespace(
        id=instance.flow_id,
        name="Release Backport",
        account_id=uuid.uuid4(),
        git_clone_config=CONFIG,
    )
    return instance


@pytest.mark.asyncio
async def test_merge_into_another_branch_does_nothing() -> None:
    subject = orchestrator(event(base="main"))
    with patch("preloop.models.crud.crud_tracker.get") as get_tracker:
        result = await subject._run_backport(PLAN)
    get_tracker.assert_not_called()
    assert result["status"] == "FAILED"
    assert "source branch release/1.0" in result["error_message"]


@pytest.mark.asyncio
async def test_bitbucket_tracker_is_refused_with_the_follow_up() -> None:
    subject = orchestrator(event())
    tracker = SimpleNamespace(tracker_type="bitbucket")
    with patch("preloop.models.crud.crud_tracker.get", return_value=tracker):
        result = await subject._run_backport(PLAN)
    assert result["status"] == "FAILED"
    assert "#955" in result["error_message"]


@pytest.mark.asyncio
async def test_backport_runs_with_the_tracker_client_and_git_token() -> None:
    subject = orchestrator(event())
    tracker = SimpleNamespace(tracker_type="github")
    client = MagicMock()
    report = BackportReport(
        change=extract_merged_change(event()),
        source_branch="release/1.0",
        targets=[
            TargetResult(
                target_branch="main",
                branch="backport/pr-812-to-main",
                status="opened",
                pull_request_url="https://github.com/acme/widgets/pull/900",
                review_request_error="Requesting reviewers failed with HTTP 422",
            )
        ],
        comment_status="posted",
    )
    run = AsyncMock(return_value=report)
    with (
        patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
        patch.object(
            subject, "_get_tracker_client_for_status", AsyncMock(return_value=client)
        ),
        patch(
            "preloop.services.flow_orchestrator.resolve_tracker_git_token",
            AsyncMock(return_value="tok"),
        ),
        patch("preloop.services.backport.run_backport", run),
        patch.object(subject, "_emit_execution_warning", AsyncMock()) as warn,
    ):
        result = await subject._run_backport(PLAN)

    args, kwargs = run.await_args
    assert args[0] == PLAN
    assert args[1].merge_commit_sha == SHA
    assert args[2].kind == "github"
    assert kwargs["token"] == "tok"
    assert kwargs["committer_email"] == "bot@example.com"
    assert result["status"] == "SUCCEEDED"
    assert result["result"]["backport"]["targets"][0]["status"] == "opened"
    warn.assert_awaited_once()
    assert "stays open" in warn.await_args.args[0]


@pytest.mark.asyncio
async def test_run_skips_the_agent_for_a_backport_flow() -> None:
    subject = orchestrator(event())
    subject.execution_log = SimpleNamespace(id=uuid.uuid4(), result=None)
    backport_result = {
        "status": "SUCCEEDED",
        "output_summary": "Backport of 812",
        "error_message": None,
        "failure_category": None,
        "result": {"backport": {"targets": []}},
    }
    with (
        patch.object(subject, "_get_flow_details"),
        patch.object(subject, "_create_execution_log"),
        patch.object(subject, "_publish_update", AsyncMock()),
        patch.object(subject, "_update_commit_status", AsyncMock()),
        patch.object(subject, "_update_execution_log", AsyncMock()) as update,
        patch.object(subject, "_prepare_execution_context", AsyncMock()) as prepare,
        patch.object(subject, "_run_agent_with_retries", AsyncMock()) as agent,
        patch.object(
            subject, "_run_backport", AsyncMock(return_value=backport_result)
        ) as run_backport,
        patch.object(subject, "_notify_terminal", AsyncMock()),
        patch.object(subject, "_start_queued_followup", AsyncMock()),
        patch.object(subject, "_retry_after_no_progress", AsyncMock()),
        patch.object(subject, "_sync_runtime_session"),
    ):
        await subject.run()

    prepare.assert_not_awaited()
    agent.assert_not_awaited()
    run_backport.assert_awaited_once()
    final = update.await_args_list[-1].kwargs
    assert final["status"] == "SUCCEEDED"
    assert final["result"]["backport"] == {"targets": []}
    assert update.await_args_list[1].kwargs["status"] == "RUNNING"
