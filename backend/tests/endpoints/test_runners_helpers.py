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


def _stub_leased_runner(execution_id: UUID) -> SimpleNamespace:
    """A runner holding exactly one halted job, with the assignment API."""
    assignment = SimpleNamespace(
        execution_id=execution_id,
        pending_job=None,
        halt_requested=True,
        reported_status="RUNNING",
    )
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        status="online",
        publication_capabilities=None,
        ephemeral=False,
        assignments=[assignment],
        capacity=1,
        free_slots=0,
    )
    runner.assignment_for = lambda wanted, runner=runner: next(
        (row for row in runner.assignments if row.execution_id == wanted), None
    )
    return runner


def _stub_capability_writer(runner: SimpleNamespace):
    """In-memory stand-in for the compare-and-swap capability write."""

    def set_publication_capabilities(
        db: object,
        *,
        runner_id: UUID,
        capabilities: dict | None,
        expected_connection_id: str | None = None,
        offline: bool = False,
        clear_lease: bool = False,
        execution_id: UUID | None = None,
        commit: bool = True,
    ) -> bool:
        current = (runner.publication_capabilities or {}).get("connection_id")
        if expected_connection_id is not None and current != expected_connection_id:
            return False
        if (
            clear_lease
            and execution_id is not None
            and runner.assignment_for(execution_id) is None
        ):
            return False
        runner.publication_capabilities = capabilities
        if clear_lease:
            runner.assignments = [
                row
                for row in runner.assignments
                if execution_id is not None and row.execution_id != execution_id
            ]
            runner.status = "offline" if offline else "online"
        return True

    return set_publication_capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, "RUNNING", "unknown", "FAILED", "STOPPED", "SUCCEEDED"]
)
async def test_completion_confirms_stop_only_on_terminal_owner_ack(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    """Invalid completion packets retain the lease and durable halt intent."""
    execution_id = uuid4()
    runner = _stub_leased_runner(execution_id)
    set_publication_capabilities = _stub_capability_writer(runner)

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
    monkeypatch.setattr(
        runners.crud_flow_execution,
        "lock_for_runner_completion",
        lambda *args, **kwargs: execution,
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
        assert runner.assignments == []
    else:
        confirm.assert_not_called()
        assert runner.assignment_for(execution_id) is not None
        assert runner.assignment_for(execution_id).halt_requested is True
        assert execution.status == "RUNNING"


_GITHUB_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "STOPPED"])
async def test_invalid_cra_completion_keeps_original_error_and_contract(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Malformed CRA on a failed/stopped complete keeps both failure reasons."""
    execution_id = uuid4()
    runner = _stub_leased_runner(execution_id)
    set_publication_capabilities = _stub_capability_writer(runner)

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
    monkeypatch.setattr(
        runners.crud_flow_execution,
        "lock_for_runner_completion",
        lambda *args, **kwargs: execution,
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


@pytest.mark.asyncio
async def test_runner_log_batch_replay_has_stable_persistence_ids(monkeypatch) -> None:
    """A lost acknowledgment can replay logs without duplicating stored markers."""
    db = MagicMock()
    execution_id, batch_id = uuid4(), uuid4()
    append = MagicMock()
    monkeypatch.setattr(runners.crud_flow_execution_log, "append_logs", append)
    monkeypatch.setattr(runners, "_publish_flow_update", AsyncMock())
    await runners.persist_runner_logs(
        db, execution_id, ["session", "PR"], str(batch_id)
    )
    await runners.persist_runner_logs(
        db, execution_id, ["session", "PR"], str(batch_id)
    )
    first, replay = [call.args[1] for call in append.call_args_list]
    assert first == replay
    assert len({entry[1]["_persistence_id"] for entry in first}) == 2


@pytest.mark.asyncio
async def test_runner_log_broadcast_timeout_does_not_block_control(monkeypatch) -> None:
    """Raw logs stay durable even when their best-effort live broadcast stalls."""
    import asyncio

    async def stalled(*args, **kwargs) -> None:
        await asyncio.Event().wait()

    append = MagicMock()
    monkeypatch.setattr(runners.crud_flow_execution_log, "append_logs", append)
    monkeypatch.setattr(runners, "_publish_flow_update", stalled)
    monkeypatch.setattr(runners, "RUNNER_LOG_BROADCAST_TIMEOUT", 0.01)
    await asyncio.wait_for(
        runners.persist_runner_logs(MagicMock(), uuid4(), ["critical marker"], None),
        timeout=0.5,
    )
    append.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_terminal_log_frame_remains_bounded_and_supported(
    monkeypatch,
) -> None:
    append = MagicMock()
    monkeypatch.setattr(runners.crud_flow_execution_log, "append_logs", append)
    monkeypatch.setattr(runners, "_publish_flow_update", AsyncMock())
    await runners.persist_runner_logs(MagicMock(), uuid4(), ["progress"] * 512, None)
    assert len(append.call_args.args[1]) == 512
    with pytest.raises(ValueError, match="Invalid runner log batch"):
        await runners.persist_runner_logs(
            MagicMock(), uuid4(), ["progress"] * 512, str(uuid4())
        )


@pytest.mark.asyncio
async def test_invalid_log_batch_error_echoes_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI needs the rejected identity to drop inflight and reclaim budget."""
    execution_id = uuid4()
    batch_id = str(uuid4())
    runner = _stub_leased_runner(execution_id)
    runner.status = "busy"
    runner.assignments[0].halt_requested = False
    set_publication_capabilities = _stub_capability_writer(runner)

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[
            {
                "type": "logs",
                "execution_id": str(execution_id),
                "batch_id": batch_id,
                "lines": ["progress"] * 512,
            },
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

    await runners.runner_ws(websocket, runner.id, MagicMock())

    errors = [
        call.args[0]
        for call in websocket.send_json.call_args_list
        if call.args and call.args[0].get("type") == "error"
    ]
    assert errors == [
        {
            "type": "error",
            "error": "Invalid runner log batch",
            "batch_id": batch_id,
        }
    ]


@pytest.mark.asyncio
async def test_hello_null_host_exec_profiles_leaves_capabilities_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hello/heartbeat with JSON null must not raise; advertisements stay empty."""
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        status="online",
        publication_capabilities=None,
        ephemeral=False,
        capabilities={"host_exec_profiles": [{"name": "stale"}]},
        assignments=[],
        capacity=1,
        free_slots=1,
        reported_concurrency=None,
    )
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[
            {"type": "heartbeat", "host_exec_profiles": None},
            WebSocketDisconnect(),
        ]
    )
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_reported_concurrency",
        lambda *args, **kwargs: runner,
    )
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _stub_capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    assert runner.capabilities == {"host_exec_profiles": []}
