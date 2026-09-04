"""Tests for flow execution worker claim, dispatch, and recovery handoff."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_recovery import ExecutionRecoveryService
from preloop.services.flow_execution_dispatcher import (
    EXECUTE_FLOW_TASK,
    dispatch_execute,
    flow_execution_worker_enabled,
)


@pytest.fixture
def flow_for_claim(db_session: Session, test_user):
    """Minimal enabled flow for claim lease tests."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"claim-test-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    db_session.commit()
    db_session.refresh(flow)
    return flow


def _create_pending_execution(db_session: Session, flow_id) -> object:
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details={"source": "test"},
        ),
    )
    db_session.commit()
    db_session.refresh(execution)
    return execution


def test_claim_execution_second_worker_loses(
    db_session: Session, flow_for_claim
) -> None:
    """Two workers: first claim wins; second loses while lease is fresh."""
    execution = _create_pending_execution(db_session, flow_for_claim.id)

    claimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-a",
        stale_after_seconds=120,
    )
    assert claimed is not None
    assert claimed.orchestrator_worker_id == "worker-a"
    assert claimed.orchestrator_heartbeat_at is not None

    lost = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-b",
        stale_after_seconds=120,
    )
    assert lost is None

    # Same worker can re-claim (heartbeat refresh path).
    again = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-a",
        stale_after_seconds=120,
    )
    assert again is not None
    assert again.orchestrator_worker_id == "worker-a"


def test_claim_execution_stale_lease_reclaimable(
    db_session: Session, flow_for_claim
) -> None:
    """Another worker can reclaim after heartbeat goes stale."""
    execution = _create_pending_execution(db_session, flow_for_claim.id)
    claimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-a",
        stale_after_seconds=120,
    )
    assert claimed is not None

    stale_time = datetime.now(timezone.utc) - timedelta(seconds=300)
    claimed.orchestrator_heartbeat_at = stale_time
    db_session.add(claimed)
    db_session.commit()

    reclaimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-b",
        stale_after_seconds=120,
    )
    assert reclaimed is not None
    assert reclaimed.orchestrator_worker_id == "worker-b"


def test_touch_heartbeat_and_release(db_session: Session, flow_for_claim) -> None:
    execution = _create_pending_execution(db_session, flow_for_claim.id)
    claimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-a",
        stale_after_seconds=120,
    )
    first_hb = claimed.orchestrator_heartbeat_at

    assert crud_flow_execution.touch_heartbeat(
        db_session, execution_id=execution.id, worker_id="worker-a"
    )
    db_session.refresh(claimed)
    assert claimed.orchestrator_heartbeat_at >= first_hb

    assert not crud_flow_execution.touch_heartbeat(
        db_session, execution_id=execution.id, worker_id="worker-other"
    )

    assert crud_flow_execution.release_claim(
        db_session, execution_id=execution.id, worker_id="worker-a"
    )
    db_session.refresh(claimed)
    assert claimed.orchestrator_worker_id is None
    assert claimed.orchestrator_heartbeat_at is None


@pytest.mark.asyncio
async def test_dispatch_execute_publishes_when_flag_on() -> None:
    execution_id = str(uuid.uuid4())
    mock_ack = MagicMock()
    mock_ack.stream = "tasks"
    mock_ack.seq = 1

    with (
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.event_bus_service.publish_task",
            new_callable=AsyncMock,
            return_value=mock_ack,
        ) as publish,
    ):
        ok = await dispatch_execute(execution_id)
        assert ok is True
        publish.assert_awaited_once_with(EXECUTE_FLOW_TASK, execution_id=execution_id)


@pytest.mark.asyncio
async def test_dispatch_execute_local_fallback_when_flag_off() -> None:
    called = {"n": 0}

    async def fallback() -> None:
        called["n"] += 1

    with patch(
        "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
        return_value=False,
    ):
        ok = await dispatch_execute(str(uuid.uuid4()), local_fallback=fallback)
        assert ok is True
        assert called["n"] == 1


@pytest.mark.asyncio
async def test_recovery_uses_redispatch_when_flag_on(
    db_session: Session,
) -> None:
    service = ExecutionRecoveryService()
    with (
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.claim_stale_after_seconds",
            return_value=120,
        ),
        patch.object(
            service,
            "_redispatch_stale_executions",
            new_callable=AsyncMock,
            return_value=3,
        ) as redisp,
    ):
        count = await service.recover_orphaned_executions(db_session)
        assert count == 3
        redisp.assert_awaited_once()
        assert redisp.await_args.kwargs["stale_after_seconds"] == 120


@pytest.mark.asyncio
async def test_redispatch_calls_execute_and_resume(
    db_session: Session, flow_for_claim
) -> None:
    pending = _create_pending_execution(db_session, flow_for_claim.id)
    running = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow_for_claim.id,
            status="RUNNING",
            trigger_event_details={},
        ),
    )
    running.agent_session_reference = "agent-session-1"
    db_session.add(running)
    db_session.commit()

    service = ExecutionRecoveryService()
    with (
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new_callable=AsyncMock,
            return_value=True,
        ) as de,
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_resume",
            new_callable=AsyncMock,
            return_value=True,
        ) as dr,
    ):
        count = await service._redispatch_stale_executions(
            db_session, stale_after_seconds=120
        )
        assert count == 2
        de.assert_awaited()
        dr.assert_awaited()
        execute_ids = {call.args[0] for call in de.await_args_list}
        resume_ids = {call.args[0] for call in dr.await_args_list}
        assert pending.id in execute_ids
        assert running.id in resume_ids


def test_flow_execution_worker_enabled_reads_settings() -> None:
    with patch("preloop.services.flow_execution_dispatcher.settings") as mock_settings:
        mock_settings.flow_execution_worker_enabled = True
        assert flow_execution_worker_enabled() is True
        mock_settings.flow_execution_worker_enabled = False
        assert flow_execution_worker_enabled() is False


@pytest.mark.asyncio
async def test_execute_upgrades_to_resume_when_session_exists(
    db_session: Session, flow_for_claim
) -> None:
    """execute_flow must not start a second agent if a session ref already exists."""
    from preloop.services import flow_execution_runner as runner

    execution = _create_pending_execution(db_session, flow_for_claim.id)
    execution.status = "RUNNING"
    execution.agent_session_reference = "existing-job"
    db_session.add(execution)
    db_session.commit()

    claimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=execution.id,
        worker_id="worker-a",
        stale_after_seconds=120,
    )
    assert claimed is not None

    resume_mock = AsyncMock()
    run_mock = AsyncMock()
    with (
        patch.object(runner, "get_orchestrator_worker_id", return_value="worker-a"),
        patch.object(runner, "claim_stale_after_seconds", return_value=120),
        patch.object(runner, "get_nats_client", new_callable=AsyncMock),
        patch.object(runner, "resume_existing_execution", resume_mock),
        patch.object(runner, "run_existing_execution", run_mock),
        patch.object(runner, "get_db_session", return_value=iter([db_session])),
        patch(
            "preloop.services.flow_execution_runner.crud_flow_execution.claim_execution",
            return_value=claimed,
        ),
        patch(
            "preloop.services.flow_execution_runner.crud_flow_execution.release_claim",
            return_value=True,
        ),
    ):
        result = await runner.claim_and_run_execution(str(execution.id), resume=False)

    assert result["status"] == "completed"
    resume_mock.assert_awaited_once()
    run_mock.assert_not_awaited()
    assert resume_mock.await_args.args[1] == "existing-job"


@pytest.mark.asyncio
async def test_evacuate_owned_claims_redispatches(
    db_session: Session, flow_for_claim
) -> None:
    from preloop.services import flow_execution_runner as runner

    running = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow_for_claim.id,
            status="RUNNING",
            trigger_event_details={},
        ),
    )
    running.agent_session_reference = "job-1"
    db_session.add(running)
    db_session.commit()

    claimed = crud_flow_execution.claim_execution(
        db_session,
        execution_id=running.id,
        worker_id="evacuate-worker",
        stale_after_seconds=120,
    )
    assert claimed is not None

    with (
        patch.object(runner, "get_db_session", return_value=iter([db_session])),
        patch.object(db_session, "close"),
        patch(
            "preloop.services.flow_execution_runner.dispatch_resume",
            new_callable=AsyncMock,
            return_value=True,
        ) as dr,
        patch(
            "preloop.services.flow_execution_runner.dispatch_execute",
            new_callable=AsyncMock,
            return_value=True,
        ) as de,
    ):
        count = await runner.evacuate_owned_claims(worker_id="evacuate-worker")

    assert count == 1
    dr.assert_awaited_once()
    de.assert_not_awaited()
    refreshed = crud_flow_execution.get(db_session, id=running.id)
    assert refreshed is not None
    assert refreshed.orchestrator_worker_id is None


@pytest.mark.asyncio
async def test_resume_terminal_merges_pr_binding_into_notify() -> None:
    """A drained resume must keep the MCP-recorded pr_url for success comments."""

    from types import SimpleNamespace

    from preloop.services.flow_execution_runner import resume_existing_execution

    orchestrator = MagicMock()
    orchestrator.execution_log = SimpleNamespace(
        id=uuid.uuid4(),
        result={"pr_url": "https://github.com/acme/app/pull/7"},
    )
    orchestrator.flow = SimpleNamespace(agent_type="codex", agent_config={})
    orchestrator.agent_type = "codex"
    orchestrator._get_flow_details = MagicMock()
    orchestrator._monitor_agent_execution = AsyncMock(
        return_value={"status": "SUCCEEDED", "result": {"ok": True}}
    )
    orchestrator._update_execution_log = AsyncMock()
    orchestrator._notify_terminal = AsyncMock()
    orchestrator._sync_runtime_session = MagicMock()
    orchestrator._revoke_execution_runtime_tokens = MagicMock()

    executor = MagicMock()
    executor.aclose = None
    executor.cleanup = None

    with patch(
        "preloop.agents.create_agent_executor",
        return_value=executor,
    ):
        await resume_existing_execution(orchestrator, "agent-session-ref")

    merged = orchestrator._notify_terminal.await_args.kwargs["result"]
    assert merged["pr_url"] == "https://github.com/acme/app/pull/7"
    assert merged["ok"] is True
    assert (
        orchestrator._update_execution_log.await_args.kwargs["result"]["pr_url"]
        == "https://github.com/acme/app/pull/7"
    )
