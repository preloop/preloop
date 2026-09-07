"""Focused tests for self-hosted runner WebSocket helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners

from preloop.api.endpoints.runners import (
    _live,
    _parse_runner_execution_id,
    _release_live_runner,
    _valid_publication_helper_image,
    job_for_heartbeat_ack,
    job_for_runner_replay,
    runner_needs_lease_token,
)
from preloop.services.websocket_manager import WebSocketManager


@pytest.mark.parametrize(
    "image",
    [
        "helper@sha256:" + "a" * 64,
        "registry.example:5000/team/helper:v1.2-rc_1@sha256:" + "0123456789abcdef" * 4,
    ],
)
def test_publication_helper_image_accepts_pinned_references(image: str) -> None:
    assert _valid_publication_helper_image(image)


@pytest.mark.parametrize(
    "image",
    [
        None,
        123,
        "",
        "helper:latest",
        "@sha256:" + "a" * 64,
        "-helper@sha256:" + "a" * 64,
        "helper@sha256:" + "A" * 64,
        "helper@sha256:" + "a" * 63,
        "helper@sha256:" + "a" * 65,
        "helper@sha256:" + "a" * 64 + "\n",
        "helper@sha256:helper@sha256:" + "a" * 64,
        "hélper@sha256:" + "a" * 64,
        "helper name@sha256:" + "a" * 64,
        "0" * 1_000_000,
        "0" * 1_000_000 + "@sha256:" + "a" * 64,
    ],
)
def test_publication_helper_image_rejects_invalid_references(image: object) -> None:
    assert not _valid_publication_helper_image(image)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, "RUNNING", "unknown", "FAILED", "STOPPED", "SUCCEEDED"]
)
async def test_completion_confirms_stop_only_on_terminal_owner_ack(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    """Invalid completion packets retain the lease and durable halt intent."""
    execution_id = uuid4()
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        current_execution_id=execution_id,
        pending_job=None,
        halt_requested=True,
        status="online",
        reported_status="RUNNING",
        publication_capabilities=None,
    )

    def set_publication_capabilities(
        db: object,
        *,
        runner_id: UUID,
        capabilities: dict | None,
        expected_connection_id: str | None = None,
        offline: bool = False,
        clear_lease: bool = False,
        execution_id: UUID | None = None,
        reported_status: str | None = None,
    ) -> bool:
        """In-memory stand-in for the compare-and-swap capability write."""
        current = (runner.publication_capabilities or {}).get("connection_id")
        if expected_connection_id is not None and current != expected_connection_id:
            return False
        if execution_id is not None and runner.current_execution_id != execution_id:
            return False
        runner.publication_capabilities = capabilities
        if clear_lease:
            runner.pending_job = None
            runner.current_execution_id = None
            runner.halt_requested = False
            runner.status = "offline" if offline else "online"
            if reported_status is not None:
                runner.reported_status = reported_status
        return True

    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        status="RUNNING",
        trigger_event_details=None,
    )
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[
            {"type": "complete", "execution_id": str(execution_id), "status": status},
            WebSocketDisconnect(),
        ]
    )
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        set_publication_capabilities,
    )
    monkeypatch.setattr(
        runners.crud_flow_execution, "get", lambda *args, **kwargs: execution
    )
    monkeypatch.setattr(runners.crud_flow, "get", lambda *args, **kwargs: None)
    confirm = MagicMock()
    monkeypatch.setattr(runners.crud_flow_execution, "confirm_stop", confirm)
    monkeypatch.setattr(
        runners.crud_flow_execution,
        "update",
        lambda db, db_obj, obj_in: db_obj,
    )
    monkeypatch.setattr(
        runners.crud_api_key, "deactivate_runtime_keys_for_flow_execution", MagicMock()
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    if status in {"SUCCEEDED", "FAILED", "STOPPED"}:
        confirm.assert_called_once()
        assert runner.current_execution_id is None
        assert runner.halt_requested is False
    else:
        confirm.assert_not_called()
        assert runner.current_execution_id == execution_id
        assert runner.halt_requested is True
        assert execution.status == "RUNNING"


_GITHUB_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "STOPPED"])
async def test_invalid_cra_completion_keeps_original_error_and_contract(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Malformed CRA on a failed/stopped complete keeps both failure reasons."""
    execution_id = uuid4()
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        current_execution_id=execution_id,
        pending_job=None,
        halt_requested=True,
        status="online",
        reported_status="RUNNING",
        publication_capabilities=None,
    )

    def set_publication_capabilities(
        db: object,
        *,
        runner_id: UUID,
        capabilities: dict | None,
        expected_connection_id: str | None = None,
        offline: bool = False,
        clear_lease: bool = False,
        execution_id: UUID | None = None,
        reported_status: str | None = None,
    ) -> bool:
        current = (runner.publication_capabilities or {}).get("connection_id")
        if expected_connection_id is not None and current != expected_connection_id:
            return False
        if execution_id is not None and runner.current_execution_id != execution_id:
            return False
        runner.publication_capabilities = capabilities
        if clear_lease:
            runner.pending_job = None
            runner.current_execution_id = None
            runner.halt_requested = False
            runner.status = "offline" if offline else "online"
            if reported_status is not None:
                runner.reported_status = reported_status
        return True

    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        status="RUNNING",
        trigger_event_details=None,
    )
    flow = SimpleNamespace(
        prompt_template=(
            "Required shape (preloop.cra.vulnscan/v1): "
            '{ "schema": "preloop.cra.vulnscan/v1" }'
        )
    )
    original = (
        f"container OOM while cloning https://{_GITHUB_PAT}@github.com/acme/app.git"
    )
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[
            {
                "type": "complete",
                "execution_id": str(execution_id),
                "status": status,
                "error": original,
                "result": {"schema": "preloop.cra.vulnscan/v1"},
            },
            WebSocketDisconnect(),
        ]
    )
    recorded: dict[str, object] = {}
    original_apply = runners.apply_runner_completion_to_execution

    def capture_completion(*args: object, **kwargs: object) -> None:
        recorded["status"] = kwargs["status"]
        recorded["error"] = kwargs["error"]
        recorded["result"] = kwargs["result"]
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        set_publication_capabilities,
    )
    monkeypatch.setattr(
        runners.crud_flow_execution, "get", lambda *args, **kwargs: execution
    )
    monkeypatch.setattr(runners.crud_flow, "get", lambda *args, **kwargs: flow)
    monkeypatch.setattr(runners.crud_flow_execution, "confirm_stop", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_execution,
        "update",
        lambda db, db_obj, obj_in: db_obj,
    )
    monkeypatch.setattr(
        runners.crud_api_key, "deactivate_runtime_keys_for_flow_execution", MagicMock()
    )
    monkeypatch.setattr(
        runners, "apply_runner_completion_to_execution", capture_completion
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    assert recorded["status"] == "FAILED"
    error = recorded["error"]
    assert isinstance(error, str)
    assert "container OOM" in error
    assert "failed contract validation" in error
    assert _GITHUB_PAT not in error
    assert "[REDACTED]" in error
    result = recorded["result"]
    assert isinstance(result, dict)
    assert result.get("error") in {"cra_result_invalid", "cra_result_missing"}
