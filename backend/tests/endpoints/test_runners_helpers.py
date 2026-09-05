"""Focused tests for self-hosted runner WebSocket helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from preloop.api.endpoints.runners import (
    _live,
    _parse_runner_execution_id,
    _release_live_runner,
    job_for_heartbeat_ack,
    job_for_runner_replay,
    runner_needs_lease_token,
)
from preloop.services.websocket_manager import WebSocketManager


def test_parse_runner_execution_id_ignores_malformed_values() -> None:
    execution_id = uuid4()

    assert _parse_runner_execution_id(str(execution_id)) == execution_id
    assert _parse_runner_execution_id("not-a-uuid") is None
    assert _parse_runner_execution_id(None) is None


def test_job_for_runner_replay_copies_stored_payload() -> None:
    pending_job = {"execution_id": str(uuid4()), "prompt": "do work"}

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

    replay = job_for_runner_replay(
        MagicMock(), pending_job=pending_job, mint_token=True
    )

    assert replay["account_api_token"] == "replay-token"
    assert replay["prompt"] == "do work"
    assert "account_api_token" not in pending_job


def test_job_for_runner_replay_skips_mint_when_disabled(monkeypatch) -> None:
    pending_job = {"execution_id": str(uuid4()), "prompt": "do work"}

    def _fail_get(*args, **kwargs):
        raise AssertionError("heartbeat replay must not load execution to mint")

    monkeypatch.setattr(
        "preloop.api.endpoints.runners.crud_flow_execution.get",
        _fail_get,
    )

    replay = job_for_runner_replay(
        MagicMock(), pending_job=pending_job, mint_token=False
    )
    assert "account_api_token" not in replay
    assert replay["prompt"] == "do work"


def test_runner_needs_lease_token_only_while_unstarted() -> None:
    assert runner_needs_lease_token(SimpleNamespace(reported_status=None))
    assert runner_needs_lease_token(SimpleNamespace(reported_status=""))
    assert runner_needs_lease_token(SimpleNamespace(reported_status="PENDING"))
    assert runner_needs_lease_token(SimpleNamespace(reported_status="pending"))
    assert not runner_needs_lease_token(SimpleNamespace(reported_status="RUNNING"))
    assert not runner_needs_lease_token(SimpleNamespace(reported_status="SUCCEEDED"))
    assert not runner_needs_lease_token(SimpleNamespace(reported_status="FAILED"))


def _patch_replay_mint(monkeypatch, token: str = "heartbeat-lease-token") -> UUID:
    execution_id = uuid4()
    flow_id = uuid4()
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
        lambda *args, **kwargs: (token, uuid4()),
    )
    return execution_id


def test_heartbeat_ack_mints_token_for_brand_new_lease(monkeypatch) -> None:
    """Cross-process delivery: online idle runner learns of a lease on heartbeat."""
    execution_id = _patch_replay_mint(monkeypatch)
    pending_job = {"execution_id": str(execution_id), "prompt": "do work"}
    runner = SimpleNamespace(pending_job=pending_job, reported_status="PENDING")

    replay = job_for_heartbeat_ack(MagicMock(), runner)

    assert replay is not None
    assert replay["account_api_token"] == "heartbeat-lease-token"
    assert replay["prompt"] == "do work"
    assert "account_api_token" not in pending_job


def test_heartbeat_ack_mints_token_when_status_not_yet_reported(monkeypatch) -> None:
    execution_id = _patch_replay_mint(monkeypatch)
    runner = SimpleNamespace(
        pending_job={"execution_id": str(execution_id), "prompt": "do work"},
        reported_status=None,
    )

    replay = job_for_heartbeat_ack(MagicMock(), runner)

    assert replay is not None
    assert replay["account_api_token"] == "heartbeat-lease-token"


def test_heartbeat_ack_skips_mint_once_running(monkeypatch) -> None:
    pending_job = {"execution_id": str(uuid4()), "prompt": "do work"}

    def _fail_get(*args, **kwargs):
        raise AssertionError("mid-execution heartbeat must not mint")

    monkeypatch.setattr(
        "preloop.api.endpoints.runners.crud_flow_execution.get",
        _fail_get,
    )
    runner = SimpleNamespace(pending_job=pending_job, reported_status="RUNNING")

    replay = job_for_heartbeat_ack(MagicMock(), runner)

    assert replay is not None
    assert "account_api_token" not in replay
    assert replay["prompt"] == "do work"


def test_heartbeat_ack_omits_job_when_idle() -> None:
    runner = SimpleNamespace(pending_job=None, reported_status=None)

    assert job_for_heartbeat_ack(MagicMock(), runner) is None


def test_runners_topic_is_subscribable() -> None:
    assert WebSocketManager.normalize_topic("runners") == "runners"
    assert WebSocketManager.resolve_topic({"type": "runner_updated"}) == "runners"
    assert (
        WebSocketManager.resolve_topic({"topic": "runners", "type": "runner_updated"})
        == "runners"
    )


def test_release_live_runner_ignores_stale_reconnect() -> None:
    old = object()
    new = object()
    _live["rid"] = new
    try:
        assert _release_live_runner("rid", old) is False
        assert _live["rid"] is new
        assert _release_live_runner("rid", new) is True
        assert "rid" not in _live
    finally:
        _live.pop("rid", None)
