"""Focused tests for self-hosted runner WebSocket helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock
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


def test_job_for_runner_replay_copies_stored_payload(monkeypatch) -> None:
    pending_job = {"execution_id": str(uuid4()), "prompt": "do work"}
    monkeypatch.setattr(
        "preloop.api.endpoints.runners.crud_flow_execution.get",
        lambda *args, **kwargs: None,
    )

    replay = job_for_runner_replay(MagicMock(), pending_job=pending_job)
    replay["prompt"] = "changed"

    assert replay is not pending_job
    assert pending_job["prompt"] == "do work"
    assert "account_api_token" not in replay


def test_job_for_runner_replay_mints_fresh_token(monkeypatch) -> None:
    execution_id = uuid4()
    flow_id = uuid4()
    pending_job = {"execution_id": str(execution_id), "prompt": "do work"}
    monkeypatch.setattr(
        "preloop.api.endpoints.runners.crud_flow_execution.get",
        lambda *args, **kwargs: SimpleNamespace(id=execution_id, flow_id=flow_id),
    )
    monkeypatch.setattr(
        "preloop.api.endpoints.runners.crud_flow.get",
        lambda *args, **kwargs: SimpleNamespace(id=flow_id),
    )
    monkeypatch.setattr(
        "preloop.services.flow_runtime_token.create_flow_runtime_token",
        lambda *args, **kwargs: ("replay-token", uuid4()),
    )

    replay = job_for_runner_replay(MagicMock(), pending_job=pending_job)

    assert replay["account_api_token"] == "replay-token"
    assert replay["prompt"] == "do work"
    assert "account_api_token" not in pending_job
