"""A parked flow survives its runner's exit and resumes on host profiles (#2598).

Also covers linking an onboarded host hook's approval to its verified
execution (#2597).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.services import approval_park, native_flow_park
from preloop.services.runner_service import hash_runner_token


class _SessionFactory:
    """``get_session_factory()`` stand-in that hands out one session."""

    def __init__(self, db: object) -> None:
        self._db = db

    def __call__(self) -> "_SessionFactory":
        return self

    def __enter__(self) -> object:
        return self._db

    def __exit__(self, *exc: object) -> bool:
        return False


# --- holds_park -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "park_request", "resumed", "stop", "expected"),
    [
        ("WAITING_FOR_HUMAN", True, False, False, True),
        ("WAITING_FOR_CHILDREN", True, False, False, True),
        ("RESUMING", True, False, False, True),
        # The runner's exit can land before the orchestrator confirms.
        ("RUNNING", True, False, False, True),
        ("RUNNING", False, False, False, False),
        # An operator stop wins over a park request.
        ("RUNNING", True, False, True, False),
        ("RUNNING", True, True, False, False),
        ("STOPPED", True, False, False, False),
    ],
)
def test_holds_park(
    status: str, park_request: bool, resumed: bool, stop: bool, expected: bool
) -> None:
    row = SimpleNamespace(
        status=status,
        park_request_id=uuid.uuid4() if park_request else None,
        resume_execution_id=uuid.uuid4() if resumed else None,
        stop_requested_at=datetime.now(UTC) if stop else None,
    )
    assert crud_flow_execution.holds_park(row) is expected


# --- runner completion on a real database ----------------------------------


def _flow(db: Session, account_id: Any, **overrides: Any) -> models.Flow:
    flow = models.Flow(
        name="Park test",
        prompt_template="test",
        agent_type=overrides.pop("agent_type", "claude_code"),
        agent_config=overrides.pop("agent_config", {"host_exec_profile": "local"}),
        account_id=account_id,
        **overrides,
    )
    db.add(flow)
    db.flush()
    return flow


def _execution(db: Session, flow: models.Flow, **values: Any) -> models.FlowExecution:
    execution = models.FlowExecution(
        flow_id=flow.id, trigger_event_details={}, **values
    )
    db.add(execution)
    db.flush()
    return execution


def _complete(db: Session, execution: models.FlowExecution, account_id: Any) -> None:
    from preloop.services.host_exec import apply_runner_completion_to_execution

    apply_runner_completion_to_execution(
        db,
        execution,
        account_id=account_id,
        status="STOPPED",
        error="Stopped by the control plane",
        result=None,
        message={"type": "complete", "status": "STOPPED"},
        pending_job={"launch_version": 1, "completion_protocol": "host_exec"},
    )
    db.flush()
    db.refresh(execution)


def test_runner_exit_keeps_a_parked_execution_waiting(db_session, test_user) -> None:
    flow = _flow(db_session, test_user.account_id)
    parked = _execution(
        db_session,
        flow,
        status="WAITING_FOR_HUMAN",
        park_request_id=uuid.uuid4(),
        parked_at=datetime.now(UTC),
    )
    _complete(db_session, parked, test_user.account_id)
    assert parked.status == "WAITING_FOR_HUMAN"
    assert parked.end_time is None


def test_runner_exit_before_park_confirm_does_not_end_the_run(
    db_session, test_user
) -> None:
    flow = _flow(db_session, test_user.account_id)
    parking = _execution(
        db_session, flow, status="RUNNING", park_request_id=uuid.uuid4()
    )
    _complete(db_session, parking, test_user.account_id)
    assert parking.status == "RUNNING"
    crud_flow_execution.confirm_park(
        db_session, execution_id=parking.id, compute_seconds=3, commit=False
    )
    db_session.refresh(parking)
    assert parking.status == "WAITING_FOR_HUMAN"


def test_runner_exit_still_stops_an_ordinary_run(db_session, test_user) -> None:
    flow = _flow(db_session, test_user.account_id)
    running = _execution(db_session, flow, status="RUNNING")
    _complete(db_session, running, test_user.account_id)
    assert running.status == "STOPPED"


# --- resume on a host profile ----------------------------------------------


def _answer(status: str = "approved") -> dict[str, Any]:
    return {
        "request_id": str(uuid.uuid4()),
        "status": status,
        "tool_name": "request_approval",
        "question": "Accept the greeting?",
        "answered_at": None,
    }


def _parked(**details: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_event_details=details,
        cli_session={"session_id": "s-1"},
        parked_compute_seconds=4,
    )


def test_parked_on_host_profile() -> None:
    host = SimpleNamespace(agent_type="claude_code", agent_config={})
    container = SimpleNamespace(agent_type="codex", agent_config={})
    pinned = SimpleNamespace(
        agent_type="codex", agent_config={"host_exec_profile": "x"}
    )
    assert approval_park.parked_on_host_profile(host, _parked())
    assert not approval_park.parked_on_host_profile(container, _parked())
    assert approval_park.parked_on_host_profile(pinned, _parked())
    # The harness pinned on the parked run wins over the flow's current one.
    routed = _parked(_model_routing={"agent_type": "claude_code"})
    assert approval_park.parked_on_host_profile(container, routed)


def test_host_profile_resume_restarts_fresh_with_the_answer() -> None:
    parked = _parked(_resume={"execution_id": "earlier"}, payload={"x": 1})
    answer = _answer()
    details = approval_park.build_resume_details(
        parked, answer, resume_mode=approval_park.RESUME_FRESH_CHECKOUT
    )
    assert "_resume" not in details
    assert details["payload"]["answers"][answer["request_id"]]["status"] == "approved"
    chain = details[approval_park.ANSWERS_KEY]
    assert chain["resumed_from_execution_id"] == str(parked.id)
    assert chain["native_resume"] is False
    assert chain["restarted_fresh"] is True
    prompt = details[approval_park.ANSWERS_PROMPT_KEY]
    assert "fresh run" in prompt
    assert "Continue from where you stopped" not in prompt


def test_container_resume_keeps_the_native_binding() -> None:
    parked = _parked()
    details = approval_park.build_resume_details(parked, _answer())
    assert details["_resume"]["execution_id"] == str(parked.id)
    assert details["_resume"]["cli_session"] == {"session_id": "s-1"}
    assert details[approval_park.ANSWERS_KEY]["restarted_fresh"] is False
    assert (
        "Continue from where you stopped" in (details[approval_park.ANSWERS_PROMPT_KEY])
    )


@pytest.mark.asyncio
async def test_decision_resumes_a_host_profile_park_as_a_fresh_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models import crud as crud_pkg
    from preloop.models.db import session as session_module

    monkeypatch.setattr(
        session_module, "get_session_factory", lambda: _SessionFactory(MagicMock())
    )
    parked = _parked(_resume={"execution_id": "earlier"})
    parked.flow_id = uuid.uuid4()
    parked.park_request_id = uuid.uuid4()
    request = SimpleNamespace(
        id=parked.park_request_id,
        account_id=uuid.uuid4(),
        status="approved",
        tool_name="request_approval",
        tool_args={"question": "Accept the greeting?"},
        approver_comment=None,
        responses=[],
        resolved_at=datetime(2026, 11, 28, tzinfo=UTC),
    )
    monkeypatch.setattr(approval_park, "_load_request", lambda _db, _id: request)
    crud_exec = MagicMock()
    crud_exec.list_parked_for_request.return_value = [parked]
    crud_exec.claim_parked_for_resume.return_value = True
    monkeypatch.setattr(crud_pkg, "crud_flow_execution", crud_exec)
    crud_flow = MagicMock()
    crud_flow.get.return_value = SimpleNamespace(
        id=parked.flow_id, agent_type="claude_code", agent_config={}
    )
    monkeypatch.setattr(crud_pkg, "crud_flow", crud_flow)
    started = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(approval_park, "_start_resume_execution", started)

    assert len(await approval_park.resume_parked_executions(request.id)) == 1
    details = started.await_args.args[3]
    assert "_resume" not in details
    assert details[approval_park.ANSWERS_KEY]["restarted_fresh"] is True


# --- #2597: verify the execution an onboarded host hook reports -------------


def _leased_host_execution(
    db: Session, test_user: Any, *, registered_by: Any = None, job: Any = None
) -> models.FlowExecution:
    flow = _flow(db, test_user.account_id)
    execution = _execution(db, flow, status="RUNNING")
    runner = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=registered_by or test_user.id,
        name="workstation",
        token_hash=hash_runner_token(f"token-{uuid.uuid4()}"),
        labels=["local"],
        status="online",
    )
    db.add(runner)
    db.flush()
    db.add(
        models.FlowRunnerAssignment(
            runner_id=runner.id,
            execution_id=execution.id,
            pending_job=job
            if job is not None
            else {"completion_protocol": "host_exec", "host_exec_profile": "local"},
        )
    )
    db.flush()
    return execution


def test_verified_host_execution_accepts_the_runner_owners_hook(
    db_session, test_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        native_flow_park, "_session_factory", lambda: _SessionFactory(db_session)
    )
    execution = _leased_host_execution(db_session, test_user)
    linked = native_flow_park.verified_host_execution(
        account_id=test_user.account_id,
        user_id=test_user.id,
        execution_id=str(execution.id),
    )
    assert linked == {"execution_id": execution.id, "flow_id": execution.flow_id}


def test_verified_host_execution_refuses_other_users_and_containers(
    db_session, test_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        native_flow_park, "_session_factory", lambda: _SessionFactory(db_session)
    )
    execution = _leased_host_execution(db_session, test_user)
    container = _leased_host_execution(
        db_session, test_user, job={"completion_protocol": "container"}
    )

    def verify(**overrides: Any) -> Any:
        kwargs = {
            "account_id": test_user.account_id,
            "user_id": test_user.id,
            "execution_id": str(execution.id),
            **overrides,
        }
        return native_flow_park.verified_host_execution(**kwargs)

    assert verify(user_id=uuid.uuid4()) is None
    assert verify(user_id=None) is None
    assert verify(account_id=uuid.uuid4()) is None
    assert verify(execution_id=str(container.id)) is None
    assert verify(execution_id="not-a-uuid") is None
    execution.status = "SUCCEEDED"
    db_session.flush()
    assert verify() is None


def test_park_messages_state_the_fresh_run_up_front() -> None:
    import json

    host = json.loads(
        approval_park.park_pending_payload(
            request_id="r",
            tool_name="request_approval",
            expires_at=None,
            console_url="https://preloop.example.com/console/approval/r",
            resume_mode=approval_park.RESUME_FRESH_CHECKOUT,
        )
    )
    assert host["resume_mode"] == "fresh_checkout"
    assert "fresh run" in host["message"]
    container = json.loads(
        approval_park.park_pending_payload(
            request_id="r",
            tool_name="request_approval",
            expires_at=None,
            console_url="https://preloop.example.com/console/approval/r",
        )
    )
    assert container["resume_mode"] == "native_session"
    native = native_flow_park.parked_message(
        "r", None, resume_mode=approval_park.RESUME_FRESH_CHECKOUT
    )
    assert "fresh run" in native and "nothing more is published" in native


def test_execution_resume_mode_reads_the_flow(db_session, test_user) -> None:
    host = _execution(db_session, _flow(db_session, test_user.account_id))
    container = _execution(
        db_session,
        _flow(db_session, test_user.account_id, agent_type="codex", agent_config={}),
    )
    mode = approval_park.execution_resume_mode
    assert mode(db_session, host.id) == approval_park.RESUME_FRESH_CHECKOUT
    assert mode(db_session, container.id) == approval_park.RESUME_NATIVE
    assert mode(db_session, uuid.uuid4()) == approval_park.RESUME_NATIVE


def test_matrix_cell_pinning_a_hosted_harness_resumes_natively() -> None:
    host_default = SimpleNamespace(
        agent_type="claude_code", agent_config={}, ai_model_id=None
    )
    cell = _parked(_matrix={"agent_type": "codex", "ai_model_id": str(uuid.uuid4())})
    assert not approval_park.parked_on_host_profile(host_default, cell)


@pytest.mark.parametrize(
    ("workspace", "mode", "phrase"),
    [
        (None, approval_park.RESUME_FRESH_CHECKOUT, "fresh checkout"),
        (
            {"kind": "authorized_directory", "id": "repo"},
            approval_park.RESUME_SAME_DIRECTORY,
            "workspace_dirty",
        ),
        (
            {"kind": "authorized_directory", "id": "repo", "allow_dirty": True},
            approval_park.RESUME_SAME_DIRECTORY_DIRTY_OK,
            "uncommitted changes included",
        ),
    ],
)
def test_host_resume_wording_matches_the_workspace_mode(
    workspace: Any, mode: str, phrase: str
) -> None:
    config: dict[str, Any] = {"host_exec_profile": "local"}
    if workspace is not None:
        config["workspace"] = workspace
    flow = SimpleNamespace(agent_type="claude_code", agent_config=config)
    assert approval_park.park_resume_mode(flow, _parked()) == mode
    assert phrase in native_flow_park.parked_message("r", None, resume_mode=mode)
    prompt = approval_park.build_resume_details(_parked(), _answer(), resume_mode=mode)[
        approval_park.ANSWERS_PROMPT_KEY
    ]
    expected = "fresh checkout" if workspace is None else "same authorized directory"
    assert expected in prompt


def test_verified_host_execution_accepts_a_durable_agent_credential(
    db_session, test_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The onboarded hook's credential has no user; the agent owner counts."""
    monkeypatch.setattr(
        native_flow_park, "_session_factory", lambda: _SessionFactory(db_session)
    )
    execution = _leased_host_execution(db_session, test_user)
    agent = SimpleNamespace(owner_user_id=test_user.id)
    monkeypatch.setattr(
        "preloop.models.crud.crud_managed_agent.get",
        lambda _db, id, account_id=None: agent,
    )
    linked = native_flow_park.verified_host_execution(
        account_id=test_user.account_id,
        user_id=None,
        execution_id=str(execution.id),
        managed_agent_id=uuid.uuid4(),
    )
    assert linked == {"execution_id": execution.id, "flow_id": execution.flow_id}
    agent.owner_user_id = uuid.uuid4()
    assert (
        native_flow_park.verified_host_execution(
            account_id=test_user.account_id,
            user_id=None,
            execution_id=str(execution.id),
            managed_agent_id=uuid.uuid4(),
        )
        is None
    )
