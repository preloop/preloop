"""Agent decision markers: parsing, storage and exposure on the execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_pr_binding import (
    MAX_DECISIONS,
    merge_result_preserving_pr_binding,
    parse_decision_marker,
    parse_decisions,
    record_decision,
    record_runner_handoff_markers,
)

TRANSCRIPT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "decision_markers"
    / "transcript.txt"
)

FIRST = {
    "summary": "Store retries on the job row",
    "reason": "The queue already locks the row, so no new table is needed",
    "alternatives": ["Separate retry table", "In-memory counter"],
}
SECOND = {
    "summary": "Cap retries at five",
    "reason": "Matches the existing webhook retry limit",
    "alternatives": [],
}


def test_transcript_yields_two_decisions_and_logs_the_malformed_line() -> None:
    with patch("preloop.services.flow_pr_binding.logger") as log:
        decisions = parse_decisions(TRANSCRIPT.read_text())

    assert decisions == [FIRST, SECOND]
    log.warning.assert_called_once()
    message, *args = log.warning.call_args.args
    assert (message % tuple(args)).startswith("Ignoring malformed PRELOOP_DECISION")


@pytest.mark.parametrize(
    "line",
    [
        "PRELOOP_DECISION",
        'PRELOOP_DECISION ["not", "an", "object"]',
        'PRELOOP_DECISION {"summary": "No reason"}',
        'PRELOOP_DECISION {"summary": " ", "reason": "blank summary"}',
        'PRELOOP_DECISION {"summary": <one line>, "reason": <why>}',
    ],
)
def test_incomplete_markers_are_ignored(line: str) -> None:
    assert parse_decision_marker(line) is None


def test_non_marker_lines_are_not_decisions() -> None:
    assert parse_decision_marker("PRELOOP_PR_OPENED {}") is None


def test_alternatives_drop_non_strings_and_text_is_bounded() -> None:
    parsed = parse_decision_marker(
        'PRELOOP_DECISION {"summary": "' + "x" * 5000 + '", "reason": "r", '
        '"alternatives": ["a", 3, null, " b "]}'
    )
    assert parsed is not None
    assert len(parsed["summary"]) == 1000
    assert parsed["alternatives"] == ["a", "b"]


def test_decisions_survive_an_agent_result_overwrite() -> None:
    existing = {
        "pr_url": "https://github.com/example/repo/pull/1",
        "decisions": [FIRST],
        "decisions_truncated": True,
    }
    incoming = {"status": "success", "decisions": [], "summary": "Done"}

    merged = merge_result_preserving_pr_binding(existing, incoming)

    assert merged["decisions"] == [FIRST]
    assert merged["decisions_truncated"] is True
    assert merged["summary"] == "Done"
    assert merged["pr_url"] == existing["pr_url"]


@pytest.fixture
def execution(db_session: Session, test_user: models.User) -> models.FlowExecution:
    flow = crud_flow.create(
        db_session,
        account_id=test_user.account_id,
        flow_in=FlowCreate(
            name="Decision flow",
            account_id=test_user.account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    return crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )


def test_record_decision_appends_once_and_caps(
    db_session: Session, execution: models.FlowExecution
) -> None:
    record_decision(db_session, execution.id, FIRST)
    record_decision(db_session, execution.id, FIRST)
    record_decision(db_session, execution.id, SECOND)
    db_session.refresh(execution)
    assert execution.result["decisions"] == [FIRST, SECOND]

    for index in range(MAX_DECISIONS):
        record_decision(
            db_session,
            execution.id,
            {"summary": f"s{index}", "reason": "r", "alternatives": []},
        )
    db_session.refresh(execution)
    assert len(execution.result["decisions"]) == MAX_DECISIONS
    assert execution.result["decisions_truncated"] is True


def test_runner_log_frames_record_decisions(
    db_session: Session, execution: models.FlowExecution
) -> None:
    for line in TRANSCRIPT.read_text().splitlines():
        record_runner_handoff_markers(
            db_session, execution, line, isolated_publication=True
        )
    db_session.refresh(execution)
    assert execution.result["decisions"] == [FIRST, SECOND]


def test_orchestrator_records_live_and_rescanned_markers_once(
    db_session: Session, execution: models.FlowExecution
) -> None:
    orchestrator: Any = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db_session
    orchestrator.execution_log = execution
    orchestrator._opened_pr = None
    orchestrator._isolated_publication_policy = None
    orchestrator.execution_logger = MagicMock()
    transcript = TRANSCRIPT.read_text()

    for line in transcript.splitlines():
        orchestrator._note_decision(line)
    orchestrator._bind_opened_pr(transcript)

    db_session.refresh(execution)
    assert execution.result["decisions"] == [FIRST, SECOND]


def test_execution_detail_exposes_decisions(
    client: Any, db_session: Session, execution: models.FlowExecution
) -> None:
    for decision in parse_decisions(TRANSCRIPT.read_text()):
        record_decision(db_session, execution.id, decision)
    execution.result = merge_result_preserving_pr_binding(
        execution.result, {"status": "success"}
    )
    db_session.flush()

    response = client.get(f"/api/v1/flows/executions/{execution.id}")

    assert response.status_code == 200
    assert response.json()["result"]["decisions"] == [FIRST, SECOND]


PRESETS_DIR = Path(__file__).resolve().parents[2] / "presets"


@pytest.mark.parametrize(
    "preset",
    ["011-automated-issue-implementation.yaml", "002-pull-request-reviewer.yaml"],
)
def test_presets_instruct_one_marker_per_decision(preset: str) -> None:
    prompt = yaml.safe_load((PRESETS_DIR / preset).read_text())["prompt_template"]
    flat = " ".join(prompt.split())

    assert "PRELOOP_DECISION {" in flat
    for key in ('"summary"', '"reason"', '"alternatives"'):
        assert key in flat
    assert "one marker per decision" in flat.lower()
    assert "non-trivial choice" in flat
    # An agent echoing its own prompt must not record the example as a decision.
    assert parse_decisions(prompt) == []
