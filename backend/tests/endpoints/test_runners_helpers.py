"""Focused tests for self-hosted runner WebSocket helpers."""

from uuid import uuid4

from preloop.api.endpoints.runners import (
    _parse_runner_execution_id,
    job_for_runner_replay,
)


def test_parse_runner_execution_id_ignores_malformed_values() -> None:
    execution_id = uuid4()

    assert _parse_runner_execution_id(str(execution_id)) == execution_id
    assert _parse_runner_execution_id("not-a-uuid") is None
    assert _parse_runner_execution_id(None) is None


def test_job_for_runner_replay_copies_stored_payload() -> None:
    pending_job = {"execution_id": str(uuid4()), "prompt": "do work"}

    replay = job_for_runner_replay(pending_job)
    replay["prompt"] = "changed"

    assert replay is not pending_job
    assert pending_job["prompt"] == "do work"
