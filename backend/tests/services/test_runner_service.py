"""Tests for self-hosted runner pool resolution and queue timeout."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.models.crud.flow_runner import crud_flow_runner, runner_matches_pool
from preloop.services.runner_service import (
    DEFAULT_QUEUE_TIMEOUT,
    hash_runner_token,
    lease_job,
    mark_queued_or_fail,
    persistable_job_payload,
    resolve_runner_pool,
)


def test_resolve_runner_pool_prefers_trigger_override() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"_runner": "office", "ref": "main"}},
    )
    assert pool == "office"


def test_resolve_runner_pool_reads_nested_payload() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"payload": {"_runner": "gpu"}}},
    )
    assert pool == "gpu"


def test_resolve_runner_pool_falls_back_to_flow() -> None:
    flow = SimpleNamespace(runner_pool="nightly")
    assert resolve_runner_pool(flow, {"trigger_event_data": {}}) == "nightly"
    assert resolve_runner_pool(SimpleNamespace(runner_pool=None), {}) is None


def test_mark_queued_or_fail_times_out() -> None:
    now = datetime.now(timezone.utc)
    assert mark_queued_or_fail(queued_since=now) == "PENDING"
    assert (
        mark_queued_or_fail(
            queued_since=now - DEFAULT_QUEUE_TIMEOUT - timedelta(seconds=1)
        )
        == "FAILED"
    )


def test_hash_runner_token_is_stable() -> None:
    assert hash_runner_token("abc") == hash_runner_token("abc")
    assert hash_runner_token("abc") != hash_runner_token("def")


def test_runner_matches_pool_id_name_or_label() -> None:
    runner_id = uuid4()
    row = SimpleNamespace(id=runner_id, name="Office Mac", labels=["local", "gpu"])
    assert runner_matches_pool(row, str(runner_id))
    assert runner_matches_pool(row, "office mac")
    assert runner_matches_pool(row, "GPU")
    assert not runner_matches_pool(row, "missing")


def test_crud_runner_counts_and_heartbeat_methods_are_reachable() -> None:
    assert hasattr(crud_flow_runner, "counts_for_instance")
    assert hasattr(crud_flow_runner, "counts_for_account")
    assert hasattr(crud_flow_runner, "touch_heartbeat")


def test_persistable_job_payload_strips_account_api_token() -> None:
    payload = {
        "execution_id": "exec-1",
        "prompt": "do work",
        "account_api_token": "live-secret",
    }
    stored = persistable_job_payload(payload)
    assert "account_api_token" not in stored
    assert stored["prompt"] == "do work"
    assert payload["account_api_token"] == "live-secret"


def test_lease_job_claims_row_and_does_not_persist_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        current_execution_id=None,
        halt_requested=False,
        reported_status=None,
    )
    claimed: list = []

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [runner],
    )

    def _claim_idle(db, *, runner_id):
        claimed.append(runner_id)
        return runner

    monkeypatch.setattr(crud_flow_runner, "claim_idle", _claim_idle)

    db = MagicMock()
    execution_id = uuid4()
    result = lease_job(
        db,
        account_id=uuid4(),
        pool="local",
        execution_id=execution_id,
        payload={
            "execution_id": str(execution_id),
            "account_api_token": "live-secret",
            "prompt": "do work",
        },
    )
    assert result is runner
    assert claimed == [runner.id]
    assert runner.pending_job is not None
    assert "account_api_token" not in runner.pending_job
    assert runner.pending_job["prompt"] == "do work"
    assert runner.status == "busy"
    assert runner.current_execution_id == execution_id
    db.commit.assert_called()


def test_lease_job_skips_locked_runner_and_claims_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = SimpleNamespace(id=uuid4(), status="online", pending_job=None)
    next_runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        current_execution_id=None,
        halt_requested=False,
        reported_status=None,
    )

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [locked, next_runner],
    )

    def _claim_idle(db, *, runner_id):
        if runner_id == locked.id:
            return None
        return next_runner

    monkeypatch.setattr(crud_flow_runner, "claim_idle", _claim_idle)

    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"prompt": "do work"},
    )
    assert result is next_runner
    assert next_runner.status == "busy"
