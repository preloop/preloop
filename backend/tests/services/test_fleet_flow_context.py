"""Fleet Map context uses compact subject metadata and real lineage only."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from preloop.services.fleet_flow_context import build_fleet_flow_context


def test_selects_subject_without_raw_content_or_title() -> None:
    trigger = {
        "source": "github",
        "type": "issue_opened",
        "payload": {"issue": {"body": "PRIVATE BODY"}},
        "_subject": {
            "repo": "example/widgets",
            "reference": "#41",
            "title": "PRIVATE TITLE",
            "text": "PRIVATE RENDERED TEXT",
            "event": "Issue Opened",
            "url": "https://example.com/issues/41",
        },
    }
    result = build_fleet_flow_context(
        flow_name="Issue triage", trigger_details=trigger, trigger_event_id="event-41"
    )
    assert result["subject"] == {
        "repo": "example/widgets",
        "reference": "#41",
        "event": "Issue Opened",
        "url": "https://example.com/issues/41",
    }
    assert result["trigger_kind"] == "issue_opened"
    assert "PRIVATE" not in json.dumps(result)
    assert "title" in trigger["_subject"]
    assert result["parent_execution_id"] is None


@pytest.mark.parametrize(
    "details", [None, [], {"payload": {"title": "PRIVATE"}}, {"_subject": []}]
)
def test_legacy_or_malformed_trigger_is_safe(details: object) -> None:
    result = build_fleet_flow_context(flow_name="Review", trigger_details=details)
    assert result["subject"] == {}
    assert "PRIVATE" not in json.dumps(result)


def test_urls_scrubbing_bounds_and_lineage() -> None:
    parent = UUID("00000000-0000-4000-8000-000000000001")
    continuation = UUID("00000000-0000-4000-8000-000000000002")
    result = build_fleet_flow_context(
        flow_name="x" * 200,
        trigger_details={
            "source": "gitlab",
            "type": "merge_request_updated",
            "_subject": {
                "reference": "!51",
                "repo": "example/widgets",
                "commit": "abcdef12",
                "url": "https://user:password@example.com/project/merge_requests/51?token=secret#part",
            },
        },
        parent_execution_id=parent,
        resume_execution_id=continuation,
    )
    assert result["subject"]["url"] == "https://example.com/project/merge_requests/51"
    assert len(result["flow_name"]) == 160
    assert result["parent_execution_id"] == str(parent)
    assert result["root_execution_id"] is None
    assert result["retry_of_execution_id"] is None
    assert result["resume_execution_id"] == str(continuation)


def test_feedback_resume_uses_existing_metadata_without_feedback_prose() -> None:
    result = build_fleet_flow_context(
        flow_name="Automated Issue Implementation",
        trigger_details={
            "source": "github",
            "type": "implementation_feedback",
            "_thread_id": "thread-41",
            "_resume": {
                "execution_id": "implementation-41",
                "resume_root": "implementation-41",
                "pr_url": "https://example.com/example/widgets/pull/51",
                "cli_session": {"secret": "PRIVATE SESSION"},
            },
            "_feedback": {
                "head_sha": "abcdef1234567890",
                "items": [{"body": "PRIVATE REVIEW"}],
            },
            "_feedback_prompt": "PRIVATE PROMPT",
        },
    )
    assert result["trigger_kind"] == "implementation_feedback"
    assert result["thread_id"] == "thread-41"
    assert result["resume_from_execution_id"] == "implementation-41"
    assert result["resume_root_execution_id"] == "implementation-41"
    assert result["resume_pr_url"] == "https://example.com/example/widgets/pull/51"
    assert result["parent_execution_id"] is None
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["execution_started", "status_update"])
async def test_publisher_attaches_context_without_copying_trigger(
    event_type: str,
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    run = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    run.nats_client = SimpleNamespace(is_connected=True, publish=AsyncMock())
    run.flow_id = UUID("00000000-0000-4000-8000-000000000003")
    run.flow = SimpleNamespace(name="Issue triage", account_id="account-1")
    run.execution_log = SimpleNamespace(
        id=UUID("00000000-0000-4000-8000-000000000004"),
        trigger_event_id="event-41",
        trigger_event_details={
            "source": "github",
            "type": "issue_opened",
            "payload": {"body": "PRIVATE"},
            "_subject": {"repo": "example/widgets", "reference": "#41"},
        },
        parent_execution_id=None,
        root_execution_id=None,
        retry_of_execution_id=None,
        resume_execution_id=None,
    )
    await run._publish_update(event_type, {"status": "RUNNING"})
    topic, encoded = run.nats_client.publish.await_args.args
    event = json.loads(encoded)
    assert topic == f"flow-updates.{run.execution_log.id}"
    assert event["account_id"] == "account-1"
    assert event["flow_id"] == str(run.flow_id)
    assert event["payload"]["status"] == "RUNNING"
    assert event["payload"]["fleet_context"]["subject"]["reference"] == "#41"
    assert "PRIVATE" not in encoded.decode()
