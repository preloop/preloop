"""Keep the private-runner completion vocabulary aligned with the CLI."""

import json
from pathlib import Path

from preloop.services.flow_orchestrator import (
    RESULT_ARTIFACT_FAILURE_STATUSES,
    RESULT_ARTIFACT_SUCCESS_STATUSES,
    RESULT_ARTIFACT_VERDICT_FAILURES,
    RESULT_ARTIFACT_VERDICT_SUCCESSES,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "runner_completion_vocabulary.json"
)


def test_result_artifact_vocabulary_matches_shared_fixture() -> None:
    vocab = json.loads(_FIXTURE.read_text())
    assert RESULT_ARTIFACT_SUCCESS_STATUSES == set(vocab["success_statuses"])
    assert RESULT_ARTIFACT_FAILURE_STATUSES == set(vocab["failure_statuses"])
    assert RESULT_ARTIFACT_VERDICT_SUCCESSES == set(vocab["success_verdicts"])
    assert RESULT_ARTIFACT_VERDICT_FAILURES == set(vocab["failure_verdicts"])
