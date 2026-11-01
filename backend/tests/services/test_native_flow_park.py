"""Approval-required native commands park unattended flow runs.

Covers the permission-check service branch for execution-scoped flow
credentials, the one-shot grant, the fail-closed resume path, and the CRUD
updates on a real database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models import models
from preloop.services import approval_park, native_flow_park
from preloop.services.agent_permission_service import request_agent_permission
from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE

SVC = "preloop.services.agent_permission_service"


def _workflow(account_id: str) -> models.ApprovalWorkflow:
    return models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Synthetic workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
        timeout_seconds=300,
    )


def _db(workflow: models.ApprovalWorkflow) -> AsyncMock:
    account = MagicMock()
    account.meta_data = {}
    setting = MagicMock()
    setting.first.return_value = (None, None)
    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, None)
    pin = MagicMock()
    pin.scalars.return_value.first.return_value = None
    default = MagicMock()
    default.scalars.return_value.first.return_value = workflow
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[setting, account_and_workflow, pin, default])
    return db


def _kwargs(account_id: str, execution_id: uuid.UUID) -> dict:
    return dict(
        base_url="http://localhost",
        account_id=account_id,
        user_id=uuid.uuid4(),
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="Agent",
        source="codex_cli",
        tool_name="Bash",
        tool_input={"command": "git push origin main"},
        agent_reasoning=None,
        client_decision=None,
        evaluation_phase="pre_tool_use",
        flow_id=uuid.uuid4(),
        flow_execution_id=execution_id,
    )


class _Patched:
    """Patch the collaborators of the require_approval path."""

    def __init__(self, *, rule: tuple | None, approval_status: str = "pending"):
        self.account_id = str(uuid.uuid4())
        self.workflow = _workflow(self.account_id)
        self.approval = MagicMock()
        self.approval.id = uuid.uuid4()
        self.approval.status = approval_status
        self.approval.approver_comment = None
        self.approval.expires_at = datetime.now(UTC) + timedelta(hours=4)
        self.service = AsyncMock()
        self.service.create_and_notify = AsyncMock(return_value=self.approval)
        self.config = MagicMock()
        self.config.id = uuid.uuid4()
        self.rule = rule
        self.window = SimpleNamespace(seconds=14400)

    def __enter__(self) -> "_Patched":
        self._patches = [
            patch(f"{SVC}.get_async_db_session"),
            patch("preloop.services.approval_service.ApprovalService"),
            patch(
                "preloop.models.crud.tool_configuration."
                "get_tool_config_by_name_and_source_async",
                new=AsyncMock(return_value=self.config),
            ),
            patch(
                f"{SVC}.apply_native_access_rules",
                new=AsyncMock(return_value=self.rule),
            ),
            patch.object(native_flow_park, "pending_park_reason", return_value=None),
            patch.object(native_flow_park, "consume_grant", return_value=None),
            patch.object(native_flow_park, "resolve_window", return_value=self.window),
            patch.object(native_flow_park, "park", return_value=True),
        ]
        started = [p.start() for p in self._patches]
        session, service_cls = started[0], started[1]
        session.return_value.__aenter__.return_value = _db(self.workflow)
        service_cls.return_value = self.service
        self.pending = started[4]
        self.grant = started[5]
        self.park = started[7]
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


REQUIRE = ("require_approval", "Pushes need a human", None, None)


@pytest.mark.asyncio
async def test_require_approval_parks_the_unattended_execution() -> None:
    """The command does not run, the run parks, the approval names the run."""
    execution_id = uuid.uuid4()
    with _Patched(rule=REQUIRE) as env:
        decision, reason, request_id, timed_out = await request_agent_permission(
            **_kwargs(env.account_id, execution_id)
        )
    assert decision == "park"
    assert request_id == str(env.approval.id)
    assert timed_out is False
    assert "did not run" in reason
    created = env.service.create_and_notify.await_args.kwargs
    assert created["execution_id"] == str(execution_id)
    assert created["timeout_seconds"] == 14400
    marker = created["tool_args"][native_flow_park.NATIVE_PARK_MARKER]
    assert marker == {
        "execution_id": str(execution_id),
        "fingerprint": native_flow_park.command_fingerprint(
            "Bash", {"command": "git push origin main"}
        ),
    }
    assert env.park.call_args.kwargs["execution_id"] == execution_id


@pytest.mark.asyncio
async def test_parked_execution_denies_further_commands_without_new_approval() -> None:
    with _Patched(rule=REQUIRE) as env:
        env.pending.return_value = "This execution is paused"
        decision, reason, request_id, _ = await request_agent_permission(
            **_kwargs(env.account_id, uuid.uuid4())
        )
    assert (decision, reason, request_id) == ("deny", "This execution is paused", None)
    env.service.create_and_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_run_spends_the_approved_command_once() -> None:
    with _Patched(rule=REQUIRE) as env:
        granted = str(uuid.uuid4())
        env.grant.return_value = granted
        decision, _reason, request_id, _ = await request_agent_permission(
            **_kwargs(env.account_id, uuid.uuid4())
        )
    assert (decision, request_id) == ("allow", granted)
    env.service.create_and_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rule_still_allows_without_parking() -> None:
    with _Patched(rule=None) as env:
        decision, *_ = await request_agent_permission(
            **_kwargs(env.account_id, uuid.uuid4())
        )
    assert decision == "allow"
    env.park.assert_not_called()
    env.service.create_and_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_that_cannot_park_is_denied_not_held() -> None:
    """An unattended hook is never held; an unparkable pending call denies."""
    with _Patched(rule=REQUIRE) as env:
        env.park.return_value = False
        req = MagicMock(status="pending", approver_comment=None)
        with patch(
            "preloop.models.crud.approval_request.get_approval_request_async",
            new=AsyncMock(return_value=req),
        ):
            decision, _reason, request_id, _ = await request_agent_permission(
                **_kwargs(env.account_id, uuid.uuid4())
            )
    assert decision == "deny"
    assert request_id == str(env.approval.id)


def test_fingerprint_ignores_preloop_markers_and_cwd() -> None:
    plain = native_flow_park.command_fingerprint("Bash", {"command": "ls"})
    marked = native_flow_park.command_fingerprint(
        "Bash",
        {"command": "ls", "cwd": "/w", "_preloop_source": "codex_cli"},
    )
    assert plain == marked
    assert plain != native_flow_park.command_fingerprint("Bash", {"command": "ls -a"})


# --- decision path -------------------------------------------------------


class _SessionFactory:
    def __init__(self, db: object) -> None:
        self._db = db

    def __call__(self) -> "_SessionFactory":
        return self

    def __enter__(self) -> object:
        return self._db

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def native_resume(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from preloop.models import crud as crud_pkg
    from preloop.models.db import session as session_module

    db = MagicMock()
    monkeypatch.setattr(
        session_module, "get_session_factory", lambda: _SessionFactory(db)
    )
    parked = SimpleNamespace(
        id=uuid.uuid4(),
        flow_id=uuid.uuid4(),
        park_request_id=uuid.uuid4(),
        trigger_event_details={},
    )
    request = SimpleNamespace(
        id=parked.park_request_id,
        account_id=uuid.uuid4(),
        status="declined",
        tool_name="Bash",
        tool_args={
            "command": "git push origin main",
            native_flow_park.NATIVE_PARK_MARKER: {
                "execution_id": str(parked.id),
                "fingerprint": "f",
            },
        },
        approver_comment=None,
        responses=[],
        resolved_at=datetime(2026, 10, 31, tzinfo=UTC),
    )
    monkeypatch.setattr(approval_park, "_load_request", lambda _db, _id: request)
    crud_exec = MagicMock()
    crud_exec.list_parked_for_request.return_value = [parked]
    crud_exec.claim_parked_for_resume.return_value = True
    crud_exec.fail_parked_for_request.return_value = True
    monkeypatch.setattr(crud_pkg, "crud_flow_execution", crud_exec)
    crud_flow = MagicMock()
    crud_flow.get.return_value = SimpleNamespace(id=parked.flow_id, name="review")
    monkeypatch.setattr(crud_pkg, "crud_flow", crud_flow)
    started = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(approval_park, "_start_resume_execution", started)
    audit = MagicMock()
    monkeypatch.setattr(native_flow_park, "_audit", audit)
    return SimpleNamespace(
        parked=parked,
        request=request,
        crud_exec=crud_exec,
        started=started,
        audit=audit,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "category"),
    [
        ("declined", "approval_denied"),
        ("cancelled", "approval_denied"),
        ("expired", "approval_timeout"),
    ],
)
async def test_refused_or_expired_native_park_fails_closed(
    native_resume: SimpleNamespace, status: str, category: str
) -> None:
    native_resume.request.status = status
    started = await approval_park.resume_parked_executions(native_resume.request.id)
    assert started == []
    native_resume.started.assert_not_awaited()
    native_resume.crud_exec.claim_parked_for_resume.assert_not_called()
    kwargs = native_resume.crud_exec.fail_parked_for_request.call_args.kwargs
    assert kwargs["failure_category"] == category
    assert kwargs["execution_id"] == native_resume.parked.id
    assert "git push origin main" in kwargs["error_message"]
    events = [c.kwargs["event"] for c in native_resume.audit.call_args_list]
    assert "execution_failed_closed" in events
    assert ("expired" in events) is (status == "expired")
    assert all(
        c.kwargs["execution_id"] == str(native_resume.parked.id)
        for c in native_resume.audit.call_args_list
    )


@pytest.mark.asyncio
async def test_approved_native_park_resumes_with_run_once_instruction(
    native_resume: SimpleNamespace,
) -> None:
    native_resume.request.status = "approved"
    started = await approval_park.resume_parked_executions(native_resume.request.id)
    assert len(started) == 1
    native_resume.crud_exec.fail_parked_for_request.assert_not_called()
    details = native_resume.started.await_args.args[3]
    prompt = details[approval_park.ANSWERS_PROMPT_KEY]
    assert "has NOT run yet" in prompt
    assert "git push origin main" in prompt
    assert "get_approval_status" not in prompt


@pytest.mark.asyncio
async def test_question_parks_keep_resuming_on_refusal(
    native_resume: SimpleNamespace,
) -> None:
    """Only native command parks fail closed; ask_user refusals still resume."""
    native_resume.request.tool_name = "ask_user"
    native_resume.request.tool_args = {"question": "Ship it?"}
    started = await approval_park.resume_parked_executions(native_resume.request.id)
    assert len(started) == 1
    native_resume.crud_exec.fail_parked_for_request.assert_not_called()


# --- real database --------------------------------------------------------


def _flow_and_execution(db_session, account_id, **execution_fields):
    flow = models.Flow(
        account_id=account_id,
        name="Native park flow",
        prompt_template="Review",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, **execution_fields)
    db_session.add(execution)
    db_session.flush()
    return flow, execution


def _approval(db_session, account_id, *, status, tool_args):
    from preloop.models.crud import crud_approval_workflow
    from preloop.models.models.tool_configuration import ToolConfiguration
    from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate

    workflow = crud_approval_workflow.create(
        db_session,
        obj_in=ApprovalWorkflowCreate(name="Native park", approval_type="manual"),
        account_id=str(account_id),
    )
    db_session.flush()
    config = ToolConfiguration(
        tool_name="Bash",
        tool_source="agent",
        account_id=account_id,
        approval_workflow_id=workflow.id,
    )
    db_session.add(config)
    db_session.flush()
    request = models.ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args=tool_args,
        status=status,
        requested_at=datetime.now(UTC),
        approval_token=f"native-park-{uuid.uuid4()}",
    )
    db_session.add(request)
    db_session.flush()
    return request


def test_native_grant_is_consumed_exactly_once(db_session, test_user) -> None:
    from preloop.models.crud import crud_approval_request

    fingerprint = native_flow_park.command_fingerprint("Bash", {"command": "make"})
    request = _approval(
        db_session,
        test_user.account_id,
        status="approved",
        tool_args={
            "command": "make",
            native_flow_park.NATIVE_PARK_MARKER: {
                "execution_id": "x",
                "fingerprint": fingerprint,
            },
        },
    )
    kwargs = dict(
        request_id=request.id,
        account_id=test_user.account_id,
        marker_key=native_flow_park.NATIVE_PARK_MARKER,
        execution_id="resumed",
        commit=False,
    )
    assert not crud_approval_request.consume_native_grant(
        db_session, fingerprint="other", **kwargs
    )
    assert crud_approval_request.consume_native_grant(
        db_session, fingerprint=fingerprint, **kwargs
    )
    assert not crud_approval_request.consume_native_grant(
        db_session, fingerprint=fingerprint, **kwargs
    )
    db_session.refresh(request)
    assert request.tool_result["native_grant"]["execution_id"] == "resumed"


def test_pending_native_grant_is_not_consumable(db_session, test_user) -> None:
    from preloop.models.crud import crud_approval_request

    fingerprint = native_flow_park.command_fingerprint("Bash", {"command": "make"})
    request = _approval(
        db_session,
        test_user.account_id,
        status="pending",
        tool_args={
            native_flow_park.NATIVE_PARK_MARKER: {"fingerprint": fingerprint},
        },
    )
    assert not crud_approval_request.consume_native_grant(
        db_session,
        request_id=request.id,
        account_id=test_user.account_id,
        marker_key=native_flow_park.NATIVE_PARK_MARKER,
        fingerprint=fingerprint,
        execution_id="resumed",
        commit=False,
    )


def test_fail_parked_for_request_fails_once_and_only_parked_rows(
    db_session, test_user
) -> None:
    from preloop.models.crud import crud_flow_execution

    request_id = uuid.uuid4()
    _flow, execution = _flow_and_execution(
        db_session,
        test_user.account_id,
        status="WAITING_FOR_HUMAN",
        park_request_id=request_id,
    )
    _flow2, running = _flow_and_execution(
        db_session,
        test_user.account_id,
        status="RUNNING",
        park_request_id=request_id,
    )
    kwargs = dict(
        approval_request_id=request_id,
        failure_category="approval_denied",
        error_message="refused",
        commit=False,
    )
    assert crud_flow_execution.fail_parked_for_request(
        db_session, execution_id=execution.id, **kwargs
    )
    assert not crud_flow_execution.fail_parked_for_request(
        db_session, execution_id=execution.id, **kwargs
    )
    assert not crud_flow_execution.fail_parked_for_request(
        db_session, execution_id=running.id, **kwargs
    )
    db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == "approval_denied"
    assert execution.end_time is not None


def test_approval_categories_are_in_the_vocabulary() -> None:
    from preloop.services.flow_failure_category import (
        FAILURE_CATEGORIES,
        FAILURE_CATEGORY_MAX_LENGTH,
        derive_failure_category,
    )

    for category in ("approval_denied", "approval_timeout", "approval_unavailable"):
        assert category in FAILURE_CATEGORIES
        assert len(category) <= FAILURE_CATEGORY_MAX_LENGTH
        assert (
            derive_failure_category(status="FAILED", explicit_category=category)
            == category
        )


# --- hosted container fail closed -------------------------------------------


def _native_rule(db_session, account_id, *, action, managed_agent_id=None):
    from preloop.models.models.tool_access_rule import ToolAccessRule
    from preloop.models.models.tool_configuration import ToolConfiguration

    config = ToolConfiguration(
        tool_name=f"Bash-{uuid.uuid4().hex[:6]}",
        tool_source="agent",
        account_id=account_id,
        managed_agent_id=managed_agent_id,
    )
    db_session.add(config)
    db_session.flush()
    db_session.add(
        ToolAccessRule(
            account_id=account_id,
            tool_configuration_id=config.id,
            condition_expression="true",
            condition_type="simple",
            action=action,
            priority=0,
            is_enabled=True,
        )
    )
    db_session.flush()
    return config


def test_hosted_gap_reports_account_wide_native_approval_rule(
    db_session, test_user
) -> None:
    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    assert native_flow_park.hosted_approval_gap(db_session, flow) is None
    _native_rule(db_session, test_user.account_id, action="deny")
    assert native_flow_park.hosted_approval_gap(db_session, flow) is None
    config = _native_rule(db_session, test_user.account_id, action="require_approval")
    reason = native_flow_park.hosted_approval_gap(db_session, flow)
    assert reason is not None
    assert config.tool_name in reason
    assert "private runner host profile" in reason


def test_hosted_gap_reports_flow_scoped_rule(db_session, test_user) -> None:
    from preloop.models.crud import crud_account

    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    account = crud_account.get(db_session, id=str(test_user.account_id))
    account.meta_data = {
        **(account.meta_data or {}),
        "subject_governance": {
            "flows": {
                str(flow.id): {
                    "tool_rules": {
                        "shell": [{"action": "require_approval", "source": "agent"}]
                    }
                }
            }
        },
    }
    db_session.add(account)
    db_session.flush()
    reason = native_flow_park.hosted_approval_gap(db_session, flow)
    assert reason is not None and "shell" in reason


def test_hosted_gap_counts_deny_rules_only_when_asked(db_session, test_user) -> None:
    """Hosted Codex makes the hook required for deny-only rules too."""
    both = ("require_approval", "deny")
    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    assert native_flow_park.hosted_approval_gap(db_session, flow, actions=both) is None
    config = _native_rule(db_session, test_user.account_id, action="deny")
    assert native_flow_park.hosted_approval_gap(db_session, flow) is None
    reason = native_flow_park.hosted_approval_gap(db_session, flow, actions=both)
    assert reason is not None and config.tool_name in reason


def test_hosted_gap_counts_scoped_deny_rules_only_when_asked(
    db_session, test_user
) -> None:
    from preloop.models.crud import crud_account

    both = ("require_approval", "deny")
    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    account = crud_account.get(db_session, id=str(test_user.account_id))
    account.meta_data = {
        **(account.meta_data or {}),
        "subject_governance": {
            "flows": {
                str(flow.id): {
                    "tool_rules": {"shell": [{"action": "deny", "source": "agent"}]}
                }
            }
        },
    }
    db_session.add(account)
    db_session.flush()
    assert native_flow_park.hosted_approval_gap(db_session, flow) is None
    reason = native_flow_park.hosted_approval_gap(db_session, flow, actions=both)
    assert reason is not None and "shell" in reason


def test_orchestrator_requires_hosted_hook_for_a_real_deny_rule(
    db_session, test_user
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    flow.agent_type = "codex"
    fake = SimpleNamespace(db=db_session, agent_type="codex", flow=flow)
    context: dict = {"agent_config": {}}
    FlowExecutionOrchestrator._refuse_hosted_approval_gap(fake, context)
    assert "native_hook_required" not in context

    _native_rule(db_session, test_user.account_id, action="deny")
    FlowExecutionOrchestrator._refuse_hosted_approval_gap(fake, context)
    assert context["native_hook_required"] is True


def test_orchestrator_refuses_hosted_launch_with_approval_gap(monkeypatch) -> None:
    from preloop.agents.errors import AgentStartError
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(
        native_flow_park, "hosted_approval_gap", lambda _db, _flow, **_kw: "refused"
    )
    fake = SimpleNamespace(
        db=None, agent_type=None, flow=SimpleNamespace(agent_type="claude_code")
    )
    with pytest.raises(AgentStartError) as raised:
        FlowExecutionOrchestrator._refuse_hosted_approval_gap(
            fake, {"agent_config": {}}
        )
    assert raised.value.category == "approval_unavailable"

    # Codex in the read-only sandbox has its shell disabled: nothing to gate.
    fake.flow.agent_type = "codex"
    FlowExecutionOrchestrator._refuse_hosted_approval_gap(
        fake, {"agent_config": {"sandbox_type": "read-only"}}
    )


def test_hosted_gap_includes_agent_pinned_native_rules(db_session, test_user) -> None:
    """The permission path resolves configs without an agent filter."""
    from preloop.models.crud import crud_managed_agent

    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    agent = crud_managed_agent.create_custom_agent(
        db_session, account_id=test_user.account_id, display_name="Pinned"
    )
    config = _native_rule(
        db_session,
        test_user.account_id,
        action="require_approval",
        managed_agent_id=agent.id,
    )
    reason = native_flow_park.hosted_approval_gap(db_session, flow)
    assert reason is not None and config.tool_name in reason


def test_hosted_gap_includes_employee_agent_scoped_rules(db_session, test_user) -> None:
    from preloop.models.crud import crud_account

    agent_id = str(uuid.uuid4())
    flow, _ = _flow_and_execution(db_session, test_user.account_id)
    flow.trigger_config = {"employee_events": {}}
    flow.agent_config = {"target_agent_id": agent_id}
    db_session.add(flow)
    account = crud_account.get(db_session, id=str(test_user.account_id))
    account.meta_data = {
        **(account.meta_data or {}),
        "subject_governance": {
            "managed_agents": {
                agent_id: {"tool_rules": {"Bash": [{"action": "require_approval"}]}}
            }
        },
    }
    db_session.add(account)
    db_session.flush()
    reason = native_flow_park.hosted_approval_gap(db_session, flow)
    assert reason is not None and "Bash" in reason


@pytest.mark.asyncio
async def test_park_marker_survives_reference_only_redaction(monkeypatch) -> None:
    from preloop.services.approval_service import ApprovalService

    service = ApprovalService(MagicMock(), "http://localhost")
    seen: dict = {}

    async def reference_only(_account_id, *, tool_name, tool_args, **_kw):
        seen["args"] = tool_args
        return {"_reference": "ref-1"}

    monkeypatch.setattr(service, "_storage_redacted_user_args", reference_only)
    marker = {"execution_id": "e", "fingerprint": "f"}
    stored = await service._storage_redacted_tool_args(
        uuid.uuid4(),
        tool_name="Bash",
        tool_args={"command": "make", native_flow_park.NATIVE_PARK_MARKER: marker},
        managed_agent_id=None,
    )
    assert stored == {
        "_reference": "ref-1",
        native_flow_park.NATIVE_PARK_MARKER: marker,
    }
    assert native_flow_park.NATIVE_PARK_MARKER not in seen["args"]
    assert native_flow_park.is_native_park(SimpleNamespace(tool_args=stored))
