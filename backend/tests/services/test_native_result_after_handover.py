"""A run finished by a different worker still selects the native answer.

A deploy drains the worker that started a run; the drain re-dispatches it as
``resume_flow_execution`` and a peer worker finishes it on a fresh
orchestrator (``resume_existing_execution``). The start-of-run decision about
native structured results lives on the orchestrator that started the run and
in the execution's ``_native_result`` record. These tests drive that hand-over
against a real database row; the agent container is a fake and no model
provider is called.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.failure_analysis import AgentFailureAnalysis
from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_user,
)
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate, FlowUpdate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.native_result import (
    NATIVE_CANDIDATES_KEY,
    NATIVE_RESULT_RECORD_KEY,
)
from tests.agents.test_native_result_repair import SCHEMA
from tests.agents.test_native_result_repair import review as make_review

SESSION = "agent-session-ref"


@pytest.fixture(autouse=True)
def _no_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(self: FlowExecutionOrchestrator) -> None:
        return None

    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_tracker_client_for_status", none
    )


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Native {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def user(db_session: Session, account: Account) -> User:
    created = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"native_{uuid4().hex[:8]}@example.com",
            "username": f"native_{uuid4().hex[:8]}",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "test_password",
            "user_source": "local",
        },
    )
    account.primary_user_id = created.id
    db_session.add(account)
    db_session.commit()
    return created


def make_flow(db: Session, account: Account, *, native: bool) -> Flow:
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"Native review {uuid4().hex[:6]}",
            description="Reviews changes with a structured answer",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
            prompt_template="Review the change",
            agent_type="codex",
            agent_config={"native_result": native},
            result_schema=SCHEMA,
            validate_result=True,
            account_id=account.id,
        ),
        account_id=account.id,
    )


def orchestrator(db: Session, flow: Flow) -> FlowExecutionOrchestrator:
    nats_client = AsyncMock()
    nats_client.is_connected = True
    return FlowExecutionOrchestrator(
        db=db, flow_id=flow.id, trigger_event_data={}, nats_client=nats_client
    )


def start(db: Session, flow: Flow) -> Any:
    """The starting worker: create the row and run the start-of-run step."""
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="RUNNING", trigger_event_details={}
        ),
    )
    crud_flow_execution.update(
        db, db_obj=execution, obj_in={"agent_session_reference": SESSION}
    )
    db.commit()
    starter = orchestrator(db, flow)
    starter._get_flow_details()
    starter.execution_log = execution
    starter._resolve_native_result()
    return execution


def fake_container(document: Any) -> AsyncMock:
    """A finished container whose capture returns ``document``."""
    fake = AsyncMock()
    fake.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
    fake.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.SUCCEEDED,
            session_reference=SESSION,
            output_summary="agent finished",
            exit_code=0,
        )
    )
    fake.get_result_artifact = AsyncMock(return_value=document)

    async def no_logs(session_reference: str) -> Any:
        if False:  # pragma: no cover - makes this an async generator
            yield

    fake.stream_logs = no_logs
    return fake


def envelope(native: Any) -> dict[str, Any]:
    return {NATIVE_CANDIDATES_KEY: {"native": native, "file": None}}


async def finish_on_peer(db: Session, flow: Flow, execution: Any, fake: Any) -> Any:
    """A peer worker after the redeploy finishes the run on a fresh object."""
    from preloop.services.flow_execution_runner import resume_existing_execution

    peer = orchestrator(db, flow)
    peer.execution_log = crud_flow_execution.get(db, id=execution.id)
    # The peer has never seen this run start.
    assert getattr(peer, "_native_result_effective", None) is None
    with (
        patch("preloop.agents.create_executor_for_execution", return_value=fake),
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.get_stop_request",
            return_value=None,
        ),
        patch.object(peer, "_listen_for_commands", AsyncMock()),
        patch.object(peer, "_stream_logs_to_nats", AsyncMock()),
        patch.object(peer, "_publish_update", AsyncMock()),
        patch.object(peer, "_park_if_requested", AsyncMock(return_value=None)),
        patch.object(peer, "_check_no_progress", AsyncMock(return_value=False)),
        patch.object(
            peer, "_sync_runtime_tool_activity_metrics", AsyncMock(return_value=None)
        ),
        patch.object(peer, "_capture_evidence_archive", AsyncMock()),
        patch.object(peer, "_capture_workspace_snapshot", AsyncMock()),
        patch.object(peer, "_notify_terminal", AsyncMock()),
        patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
    ):
        peer._agent_exec_started = True
        await resume_existing_execution(peer, SESSION)
    stored = crud_flow_execution.get(db, id=execution.id)
    db.refresh(stored)
    return stored


@pytest.mark.asyncio
async def test_peer_worker_selects_the_native_answer(
    db_session: Session, account: Account, user: User
) -> None:
    flow = make_flow(db_session, account, native=True)
    execution = start(db_session, flow)
    record = execution.trigger_event_details[NATIVE_RESULT_RECORD_KEY]
    assert record == {"value": True, "source": "flow"}

    answer = make_review()
    stored = await finish_on_peer(
        db_session, flow, execution, fake_container(envelope(answer))
    )

    assert stored.status == "SUCCEEDED", stored.error_message
    assert stored.failure_category is None
    assert stored.result == answer
    assert NATIVE_CANDIDATES_KEY not in (stored.result or {})
    assert stored.result_schema_validation["valid"] is True


@pytest.mark.asyncio
async def test_peer_worker_without_a_record_resolves_from_the_flow(
    db_session: Session, account: Account, user: User
) -> None:
    """A row written before the record existed still gets the flow's setting."""
    flow = make_flow(db_session, account, native=True)
    execution = start(db_session, flow)
    details = dict(execution.trigger_event_details)
    details.pop(NATIVE_RESULT_RECORD_KEY)
    crud_flow_execution.update(
        db_session, db_obj=execution, obj_in={"trigger_event_details": details}
    )
    db_session.commit()

    answer = make_review()
    stored = await finish_on_peer(
        db_session, flow, execution, fake_container(envelope(answer))
    )

    assert stored.status == "SUCCEEDED", stored.error_message
    assert stored.result == answer


@pytest.mark.asyncio
async def test_peer_worker_without_native_results_is_unchanged(
    db_session: Session, account: Account, user: User
) -> None:
    flow = make_flow(db_session, account, native=False)
    execution = start(db_session, flow)
    assert execution.trigger_event_details[NATIVE_RESULT_RECORD_KEY]["value"] is False

    answer = make_review()
    stored = await finish_on_peer(db_session, flow, execution, fake_container(answer))

    assert stored.status == "SUCCEEDED", stored.error_message
    assert stored.result == answer
    assert stored.result_schema_validation["valid"] is True


@pytest.mark.asyncio
async def test_an_envelope_is_never_stored_as_the_result(
    db_session: Session, account: Account, user: User
) -> None:
    """Even when the stored decision is off, the transport is not a result."""
    flow = make_flow(db_session, account, native=False)
    execution = start(db_session, flow)

    stored = await finish_on_peer(
        db_session, flow, execution, fake_container(envelope(make_review()))
    )

    assert stored.status == "FAILED"
    assert stored.result is None or NATIVE_CANDIDATES_KEY not in stored.result
    validation = stored.result_schema_validation or {}
    paths = {error.get("path") for error in validation.get("errors") or []}
    assert "$.findings" not in paths
    assert "native result transport" in (stored.error_message or "")
    assert stored.failure_category == "result_selection_failed"


def test_result_selection_failure_is_not_model_transient() -> None:
    """Provider noise in the log analysis cannot outrank our own failure."""
    from preloop.services.flow_result_schema import NATIVE_SELECTION_UNAVAILABLE

    category = derive_failure_category(
        status="FAILED",
        error_message=f"{NATIVE_SELECTION_UNAVAILABLE}; the run did not confirm",
        failure_analysis={"error_class": "upstream_disconnect", "transient": True},
    )
    assert category == "result_selection_failed"


@pytest.mark.asyncio
async def test_resumed_run_keeps_the_monitor_failure_category(
    db_session: Session, account: Account, user: User
) -> None:
    """The peer stores the category the monitor named, as the first worker does."""
    flow = make_flow(db_session, account, native=True)
    execution = start(db_session, flow)
    progress_note = {"status": "in_progress"}
    fake = fake_container(envelope(progress_note))
    # Codex logs routinely carry a reconnect line; the analysis calls it
    # transient even though the agent went on to finish.
    fake.get_result.return_value.failure_analysis = AgentFailureAnalysis(
        message="stream disconnected, reconnecting",
        transient=True,
        error_class="upstream_disconnect",
    )
    stored = await finish_on_peer(db_session, flow, execution, fake)

    assert stored.status == "FAILED"
    assert stored.failure_category == "result_schema_invalid", stored.error_message


@pytest.mark.asyncio
async def test_handover_of_the_reported_run_succeeds(
    db_session: Session, account: Account, user: User
) -> None:
    """The stored record: a finished native review flagged by the log scan.

    The container exited 0 with a complete answer, but its logs carried a
    provider reconnect line, so the executor reported FAILED with a
    transient analysis. The starting worker lets a valid native answer
    supersede that heuristic; the peer must do the same instead of storing
    the transport as a ``model_transient`` failure.
    """
    flow = make_flow(db_session, account, native=True)
    execution = start(db_session, flow)
    answer = make_review()
    fake = fake_container(envelope(answer))
    fake.get_result.return_value = AgentExecutionResult(
        status=AgentStatus.FAILED,
        session_reference=SESSION,
        output_summary="agent finished",
        error_message="stream disconnected before completion",
        exit_code=0,
        log_failure_override=True,
        failure_analysis=AgentFailureAnalysis(
            message="stream disconnected before completion",
            transient=True,
            error_class="upstream_disconnect",
        ),
    )

    stored = await finish_on_peer(db_session, flow, execution, fake)

    assert stored.failure_category != "model_transient"
    assert stored.status == "SUCCEEDED", stored.error_message
    assert stored.result == answer
    assert stored.result_schema_validation["valid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("started_with", [True, False])
async def test_peer_follows_the_stored_decision_over_a_later_flow_edit(
    db_session: Session, account: Account, user: User, started_with: bool
) -> None:
    """The decision the run started with wins over the flow's current setting."""
    flow = make_flow(db_session, account, native=started_with)
    execution = start(db_session, flow)
    crud_flow.update(
        db_session,
        db_obj=flow,
        flow_in=FlowUpdate(agent_config={"native_result": not started_with}),
        account_id=account.id,
    )
    db_session.commit()
    db_session.refresh(flow)
    assert flow.agent_config["native_result"] is not started_with

    answer = make_review()
    document = envelope(answer) if started_with else answer
    stored = await finish_on_peer(db_session, flow, execution, fake_container(document))

    assert stored.status == "SUCCEEDED", stored.error_message
    assert stored.result == answer
