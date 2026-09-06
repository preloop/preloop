"""Transactional halt, deadline preservation and managed cancellation regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account_halt,
    crud_approval_request,
    crud_flow_execution,
)
from preloop.services import kill_switch
from preloop.services.approval_service import ApprovalService
from preloop.services.dynamic_fastmcp import DynamicFastMCP


@pytest.fixture(autouse=True)
def clear_cache():
    kill_switch.invalidate_kill_switch_cache()
    yield
    kill_switch.invalidate_kill_switch_cache()


def test_invalidation_during_lookup_discards_stale_fill():
    def read(*args, **kwargs):
        kill_switch.invalidate_kill_switch_cache("owner")
        return set()

    with patch.object(
        kill_switch.crud_account_halt, "active_scopes", side_effect=[set(), {"flows"}]
    ) as lookup:
        with patch.object(kill_switch.time, "monotonic", side_effect=[0, 6]):
            assert kill_switch.halted_scopes(MagicMock(), "owner") == set()
            assert kill_switch.halted_scopes(MagicMock(), "owner") == {"flows"}
        assert lookup.call_count == 2

    kill_switch.invalidate_kill_switch_cache()
    with patch.object(
        kill_switch.crud_account_halt, "active_scopes", side_effect=[set(), {"tools"}]
    ) as lookup:
        original = lookup.side_effect

        def racing_read(*args, **kwargs):
            result = next(original)
            if not result:
                kill_switch.invalidate_kill_switch_cache("owner")
            return result

        lookup.side_effect = racing_read
        assert kill_switch.halted_scopes(MagicMock(), "owner") == {"tools"}
        assert kill_switch.halted_scopes(MagicMock(), "owner") == {"tools"}
        assert lookup.call_count == 2


def test_cache_is_bounded_and_keeps_recent_accounts():
    with (
        patch.object(kill_switch, "KILL_SWITCH_CACHE_MAX_ENTRIES", 2),
        patch.object(
            kill_switch.crud_account_halt, "active_scopes", return_value=set()
        ) as lookup,
    ):
        for owner in ("a", "b", "a", "c", "a"):
            kill_switch.halted_scopes(MagicMock(), owner)
        assert lookup.call_count == 3
        assert list(kill_switch._CACHE) == ["c", "a"]
        kill_switch.halted_scopes(MagicMock(), "b")
        assert lookup.call_count == 4


@pytest.fixture
def execution(db_session, test_user):
    flow = models.Flow(
        account_id=test_user.account_id,
        name="halt durability",
        agent_type="codex",
        prompt_template="task",
        trigger_event_source="manual",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="PENDING")
    db_session.add(execution)
    db_session.commit()
    return execution


def test_admission_before_activation_is_durably_stopped_after_reenable(
    db_session, test_user, execution
):
    assert crud_flow_execution.admit_runtime_start(
        db_session, execution_id=execution.id
    )
    crud_account_halt.transition_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=True,
        user_id=test_user.id,
        reason="incident",
    )
    crud_account_halt.transition_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=False,
        user_id=test_user.id,
        reason="recovered",
    )
    db_session.refresh(execution)
    assert execution.stop_requested_at is not None
    assert execution.stop_confirmed_at is None
    assert execution.stop_source == "account_halt"
    assert execution.stop_reason == "incident"
    assert not crud_flow_execution.admit_runtime_start(
        db_session, execution_id=execution.id
    )
    halt = crud_account_halt.get_for_scope(
        db_session, account_id=test_user.account_id, scope="flows"
    )
    assert halt.deactivation_reason == "recovered"


def test_activation_before_admission_blocks_launch_and_pending_churn(
    db_session, test_user, execution
):
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=True,
        user_id=test_user.id,
    )
    assert not crud_flow_execution.admit_runtime_start(
        db_session, execution_id=execution.id
    )
    assert (
        crud_flow_execution.claim_execution(
            db_session, execution_id=execution.id, worker_id="test"
        )
        is None
    )
    assert execution.id not in {
        row.id for row in crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    }


def test_recovery_keeps_unconfirmed_runtime_stop_during_halt(
    db_session, test_user, execution
):
    execution.status = "RUNNING"
    execution.agent_session_reference = "runner:queued:local:unused"
    db_session.commit()
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=True,
        user_id=test_user.id,
    )
    # Even an earlier monitor failure must not orphan the runtime stop request.
    execution.status = "FAILED"
    db_session.commit()
    assert execution.id in {
        row.id for row in crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    }
    assert (
        crud_flow_execution.claim_execution(
            db_session, execution_id=execution.id, worker_id="new-worker"
        )
        is not None
    )
    crud_flow_execution.confirm_stop(db_session, execution_id=execution.id)
    assert execution.id not in {
        row.id for row in crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    }


@pytest.fixture
def pending_approvals(db_session, test_user):
    start = datetime(2026, 9, 6, 12)
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id, name="deadline test", workflow_type="simple"
    )
    tool = models.ToolConfiguration(
        account_id=test_user.account_id, tool_name="halt-test", tool_source="builtin"
    )
    db_session.add_all([workflow, tool])
    db_session.flush()
    rows = []
    for requested, expires in [(-100, 20), (-100, -1), (10, 130)]:
        row = models.ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            tool_name="halt-test",
            tool_args={},
            requested_at=start + timedelta(seconds=requested),
            expires_at=start + timedelta(seconds=expires),
            status="pending",
        )
        db_session.add(row)
        rows.append(row)
    db_session.commit()
    return start, rows


def test_unpolled_deadlines_recover_once_preserving_original_remaining_time(
    db_session, test_user, pending_approvals
):
    start, rows = pending_approvals

    def transition(active, seconds):
        crud_account_halt.set_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["tools"],
            active=active,
            user_id=test_user.id,
            now=(start + timedelta(seconds=seconds)).replace(tzinfo=timezone.utc),
        )

    transition(True, 0)
    transition(True, 15)  # Idempotent confirmation must not reset freeze start.
    assert (
        crud_approval_request.expire_stale_pending(
            db_session,
            account_id=str(test_user.account_id),
            now=start + timedelta(seconds=140),
        )
        == 1
    )
    transition(False, 200)
    transition(False, 250)  # Repeated recovery must not extend again.
    for row in rows:
        db_session.refresh(row)
    assert rows[0].expires_at == start + timedelta(seconds=220)
    assert rows[1].expires_at == start - timedelta(seconds=1)
    assert rows[1].status == "expired"
    assert rows[2].expires_at == start + timedelta(seconds=320)


@pytest.mark.asyncio
async def test_expired_before_halt_is_not_actionable():
    start = datetime.now(timezone.utc)
    db = MagicMock(
        run_sync=AsyncMock(
            return_value=SimpleNamespace(is_active=True, activated_at=start)
        )
    )
    service = ApprovalService(db, "https://test.invalid")
    assert not await service._approvals_frozen(
        uuid.uuid4(), start.replace(tzinfo=None) - timedelta(seconds=1)
    )
    assert await service._approvals_frozen(
        uuid.uuid4(), start.replace(tzinfo=None) + timedelta(seconds=1)
    )


@pytest.mark.asyncio
async def test_replay_uses_explicit_account_without_http_context():
    mcp = DynamicFastMCP("explicit-owner")
    mcp._user_context_provider = lambda: None
    mcp._halt_dispatch_denial = AsyncMock(return_value="owner halted")
    result = await mcp.call_registered_tool_without_policy(
        "write", {}, account_id="approval-owner"
    )
    mcp._halt_dispatch_denial.assert_awaited_once_with("approval-owner")
    assert result.content[0].text == "owner halted"


def test_first_activation_and_audit_serialize_and_rollback(db_engine):
    # Independent committed account makes real separate-session concurrency
    # possible; test_user intentionally lives in a rollback-only transaction.
    account_id = uuid.uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="halt-race"))
        db.commit()

    def activate(reason):
        with Session(db_engine) as db:
            crud_account_halt.transition_scopes(
                db,
                account_id=account_id,
                scopes=["tools"],
                active=True,
                user_id=None,
                reason=reason,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(activate, ["one", "two"]))
        with Session(db_engine) as db:
            assert (
                len(crud_account_halt.list_for_account(db, account_id=account_id)) == 1
            )
            audit = (
                db.query(models.AuditLog)
                .filter(models.AuditLog.account_id == account_id)
                .all()
            )
            assert sorted(row.details["changed"] for row in audit) == [False, True]
            with (
                patch(
                    "preloop.models.crud.account_halt.CRUDAccountHalt.set_scopes",
                    wraps=crud_account_halt.set_scopes,
                ),
                patch(
                    "preloop.models.crud.audit_log.crud_audit_log.log_action",
                    side_effect=RuntimeError("audit storage failed"),
                ),
            ):
                with pytest.raises(RuntimeError, match="audit storage failed"):
                    crud_account_halt.transition_scopes(
                        db,
                        account_id=account_id,
                        scopes=["tools"],
                        active=False,
                        user_id=None,
                        reason="attempt",
                    )
            db.expire_all()
            assert crud_account_halt.active_scopes(db, account_id=account_id) == {
                "tools"
            }
    finally:
        with Session(db_engine) as db:
            db.query(models.Account).filter(models.Account.id == account_id).delete()
            db.commit()


def test_cache_fails_closed_after_bounded_concurrent_invalidations():
    def changing(*args, **kwargs):
        kill_switch.invalidate_kill_switch_cache("unrelated-account")
        return set()

    with patch.object(
        kill_switch.crud_account_halt, "active_scopes", side_effect=changing
    ) as lookup:
        with pytest.raises(RuntimeError, match="changed repeatedly"):
            kill_switch.halted_scopes(MagicMock(), "owner")
    assert lookup.call_count == 3
    assert not kill_switch._CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected",
    [
        ({"Running": True, "Status": "running"}, False),
        ({"Running": False, "Status": "paused"}, False),
        ({"Running": False, "Status": "exited"}, True),
    ],
)
async def test_docker_stop_confirmation_requires_inspected_termination(state, expected):
    from preloop.agents.container import ContainerAgentExecutor

    executor = ContainerAgentExecutor("codex", {}, "python:3.12")
    executor.use_kubernetes = False
    container = SimpleNamespace(show=AsyncMock(return_value={"State": state}))
    executor._get_docker_client = AsyncMock(
        return_value=SimpleNamespace(
            containers=SimpleNamespace(get=AsyncMock(return_value=container))
        )
    )
    assert await executor.is_stopped("owned-container") is expected


@pytest.mark.asyncio
async def test_docker_lookup_failure_never_confirms_stop():
    from preloop.agents.container import ContainerAgentExecutor

    executor = ContainerAgentExecutor("codex", {}, "python:3.12")
    executor.use_kubernetes = False
    executor._get_docker_client = AsyncMock(
        side_effect=RuntimeError("runtime unavailable")
    )
    with pytest.raises(RuntimeError, match="runtime unavailable"):
        await executor.is_stopped("owned-container")


@pytest.mark.asyncio
async def test_kubernetes_stop_confirmation_waits_for_residual_pods():
    from kubernetes_asyncio.client import ApiException
    from preloop.agents.container import ContainerAgentExecutor

    executor = ContainerAgentExecutor("codex", {}, "python:3.12")
    executor.use_kubernetes = True
    executor._init_kubernetes_clients = AsyncMock()
    executor._k8s_batch_api = SimpleNamespace(
        read_namespaced_job_status=AsyncMock(side_effect=ApiException(status=404))
    )
    executor._k8s_core_api = SimpleNamespace(
        list_namespaced_pod=AsyncMock(return_value=SimpleNamespace(items=[object()]))
    )
    assert not await executor.is_stopped("owned-job")
    executor._k8s_core_api.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    assert await executor.is_stopped("owned-job")
    executor._k8s_core_api.list_namespaced_pod.side_effect = ApiException(status=503)
    with pytest.raises(ApiException):
        await executor.is_stopped("owned-job")


@pytest.mark.asyncio
async def test_private_runner_request_is_not_stop_confirmation(
    db_session, test_user, execution
):
    from preloop.agents.remote_runner import RemoteRunnerExecutor
    from preloop.agents.base import AgentStatus

    runner = models.FlowRunner(
        account_id=test_user.account_id,
        name="halt-runner",
        token_hash="local-test",
        status="busy",
        current_execution_id=execution.id,
        halt_requested=True,
        reported_status="RUNNING",
    )
    db_session.add(runner)
    db_session.flush()
    execution.agent_session_reference = f"runner:{runner.id}:{execution.id}"
    execution.status = "RUNNING"
    db_session.commit()
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=True,
        user_id=test_user.id,
    )
    executor = RemoteRunnerExecutor(
        "codex", {}, db=db_session, pool="local", account_id=test_user.account_id
    )
    await executor.stop(execution.agent_session_reference)
    assert not await executor.is_stopped(execution.agent_session_reference)
    assert (
        await executor.get_status(execution.agent_session_reference)
        == AgentStatus.RUNNING
    )
    crud_flow_execution.confirm_stop(db_session, execution_id=execution.id)
    assert await executor.is_stopped(execution.agent_session_reference)


@pytest.mark.asyncio
async def test_monitor_checks_stop_on_first_poll_but_not_generic_failed_status(
    db_session, test_user, execution
):
    from preloop.agents.base import AgentStatus
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    execution.status = "RUNNING"
    execution.agent_session_reference = "owned-container"
    db_session.commit()
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["flows"],
        active=True,
        user_id=test_user.id,
    )
    orch = FlowExecutionOrchestrator(
        db_session, flow_id=execution.flow_id, trigger_event_data={}, nats_client=None
    )
    orch.execution_log = execution
    orch.execution_logger = MagicMock()
    orch._listen_for_commands = AsyncMock()
    orch._stream_logs_to_nats = AsyncMock()
    orch._cleanup_monitoring = AsyncMock()
    orch._capture_result_artifact = AsyncMock(return_value=None)
    executor = SimpleNamespace(
        stop=AsyncMock(),
        is_stopped=AsyncMock(side_effect=[False, True]),
        get_status=AsyncMock(return_value=AgentStatus.FAILED),
    )
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        assert (
            crud_flow_execution.get_stop_request(db_session, execution_id=execution.id)[
                "confirmed_at"
            ]
            is None
        )

    with patch("preloop.services.flow_orchestrator.asyncio.sleep", side_effect=sleep):
        result = await orch._monitor_agent_execution("owned-container", executor)
    assert result["status"] == "STOPPED"
    assert executor.stop.await_count == 2
    executor.get_status.assert_not_awaited()
    assert sleeps == [5]
    assert (
        crud_flow_execution.get_stop_request(db_session, execution_id=execution.id)[
            "confirmed_at"
        ]
        is not None
    )


@pytest.mark.parametrize("activation_first", [True, False])
def test_account_lock_orders_concurrent_activation_and_launch(
    db_engine, activation_first
):
    account_id, flow_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="launch-race"))
        db.flush()
        db.add(
            models.Flow(
                id=flow_id,
                account_id=account_id,
                name="race",
                agent_type="codex",
                prompt_template="task",
                trigger_event_source="manual",
                agent_config={},
            )
        )
        db.flush()
        db.add(models.FlowExecution(id=execution_id, flow_id=flow_id, status="PENDING"))
        db.commit()
    started = Event()

    def contender():
        with Session(db_engine) as db:
            started.set()
            if activation_first:
                return crud_flow_execution.admit_runtime_start(
                    db, execution_id=execution_id
                )
            crud_account_halt.set_scopes(
                db, account_id=account_id, scopes=["flows"], active=True, user_id=None
            )
            return True

    try:
        with Session(db_engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
            if activation_first:
                crud_account_halt.set_scopes(
                    first,
                    account_id=account_id,
                    scopes=["flows"],
                    active=True,
                    user_id=None,
                    commit=False,
                )
            else:
                assert crud_flow_execution.admit_runtime_start(
                    first, execution_id=execution_id, commit=False
                )
            task = pool.submit(contender)
            assert started.wait(2)
            assert not task.done()
            first.commit()
            assert task.result(timeout=5) is not activation_first
        with Session(db_engine) as db:
            request = crud_flow_execution.get_stop_request(
                db, execution_id=execution_id
            )
            assert (request is None) is activation_first
    finally:
        with Session(db_engine) as db:
            db.query(models.FlowExecution).filter(
                models.FlowExecution.id == execution_id
            ).delete()
            db.query(models.Flow).filter(models.Flow.id == flow_id).delete()
            db.query(models.Account).filter(models.Account.id == account_id).delete()
            db.commit()


@pytest.mark.asyncio
async def test_cached_allow_reuses_session_and_skips_fresh_database_lookup():
    from contextlib import asynccontextmanager
    from preloop.services.approval_helper import require_approval

    opened = []

    @asynccontextmanager
    async def session_factory():
        opened.append(True)
        yield SimpleNamespace(
            run_sync=AsyncMock(side_effect=lambda callback: callback(MagicMock()))
        )

    with patch.object(
        kill_switch.crud_account_halt, "active_scopes", return_value=set()
    ) as lookup:
        kill_switch.halted_scopes(MagicMock(), "owner")
        with (
            patch("preloop.models.db.session.get_async_db_session", session_factory),
            patch(
                "preloop.models.crud.tool_configuration.get_tool_config_by_name_and_source_async",
                new=AsyncMock(return_value=None),
            ),
        ):
            assert await require_approval("read", "builtin", "owner", {}) == (True, "")
        assert len(opened) == 1
        assert lookup.call_count == 1


@pytest.mark.asyncio
async def test_frozen_poll_releases_database_before_sleep():
    from contextlib import asynccontextmanager

    held = 0
    request = SimpleNamespace(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        status="pending",
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )

    @asynccontextmanager
    async def session_factory():
        nonlocal held
        held += 1
        try:
            yield MagicMock()
        finally:
            held -= 1

    async def sleep(seconds):
        assert held == 0
        raise InterruptedError("poll checked")

    with (
        patch("preloop.models.db.session.get_async_db_session", session_factory),
        patch.object(
            ApprovalService,
            "get_approval_request_for_update",
            new=AsyncMock(return_value=request),
        ),
        patch.object(
            ApprovalService, "_approvals_frozen", new=AsyncMock(return_value=True)
        ),
        patch("preloop.services.approval_service.asyncio.sleep", side_effect=sleep),
    ):
        with pytest.raises(InterruptedError, match="poll checked"):
            await ApprovalService(
                MagicMock(), "https://test.invalid"
            ).wait_for_approval(request.id)


@pytest.mark.asyncio
async def test_live_docker_halt_stops_process_after_scope_reenabled(
    db_session, test_user, execution
):
    """Opt-in disposable local runtime test; never follows a remote Docker context."""
    import os
    import aiodocker
    from preloop.agents.container import ContainerAgentExecutor
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    socket = os.environ.get("PRELOOP_TEST_DOCKER_STOP_SOCKET")
    if not socket:
        pytest.skip("opt-in disposable local Docker runtime")
    assert socket.startswith("unix://"), (
        "Only an explicitly selected local socket is permitted"
    )
    client = aiodocker.Docker(url=socket)
    container = await client.containers.create(
        config={
            "Image": "ubuntu:24.04",
            "Cmd": ["/bin/sh", "-c", "trap 'exit 0' TERM; while :; do sleep 1; done"],
            "Labels": {"preloop-test": "pr443", "execution_id": str(execution.id)},
        }
    )
    try:
        await container.start()
        execution.status = "RUNNING"
        execution.agent_session_reference = container.id
        db_session.commit()
        crud_account_halt.transition_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["flows"],
            active=True,
            user_id=test_user.id,
            reason="local termination drill",
        )
        crud_account_halt.transition_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["flows"],
            active=False,
            user_id=test_user.id,
            reason="verify durable intent",
        )
        executor = ContainerAgentExecutor("codex", {}, "ubuntu:24.04")
        executor.use_kubernetes = False
        executor._get_docker_client = AsyncMock(return_value=client)
        orch = FlowExecutionOrchestrator(
            db_session,
            flow_id=execution.flow_id,
            trigger_event_data={},
            nats_client=None,
        )
        orch.execution_log = execution
        orch.execution_logger = MagicMock()
        orch._listen_for_commands = AsyncMock()
        orch._stream_logs_to_nats = AsyncMock()
        orch._capture_result_artifact = AsyncMock(return_value=None)
        result = await orch._monitor_agent_execution(container.id, executor)
        assert result["status"] == "STOPPED"
        assert (await container.show())["State"]["Running"] is False
        assert (
            crud_flow_execution.get_stop_request(db_session, execution_id=execution.id)[
                "confirmed_at"
            ]
            is not None
        )
    finally:
        await container.delete(force=True)
        await client.close()
