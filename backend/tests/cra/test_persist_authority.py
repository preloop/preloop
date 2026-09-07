"""Actual ApprovalRequest rows must authenticate human CRA decisions."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from preloop.cra.persist import apply_cra_persist_boundary, load_platform_approvals
from preloop.cra.validate import AUTHORITY_REQUIRED
from preloop.models import models

from .conftest import clone
from .test_validate import _kev_finding, _release_with_kevs, _waiver


def _workflow_and_config(db_session: Any, test_user: Any) -> tuple[Any, Any]:
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name=f"cra-authority-{uuid.uuid4().hex[:8]}",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="ask_user",
        tool_source="mcp",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()
    return workflow, config


def _add_request(
    db_session: Any,
    *,
    test_user: Any,
    workflow: Any,
    config: Any,
    execution_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    status: str = "approved",
    tool_result: dict[str, Any] | None = None,
    responses: list[dict[str, Any]] | None = None,
    approver_comment: str | None = None,
    decided_by_ai: bool = False,
    auto_approved_reason: str | None = None,
    resolved_at: datetime | None = None,
) -> Any:
    resolved = resolved_at or datetime(2026, 8, 20, 12, 0, 0)
    row = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        execution_id=execution_id,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        responses=responses,
        approver_comment=approver_comment,
        status=status,
        decided_by_ai=decided_by_ai,
        auto_approved_reason=auto_approved_reason,
        resolved_at=resolved,
    )
    db_session.add(row)
    db_session.flush()
    db_session.refresh(row)
    return row


def test_human_ask_user_row_may_waive(
    db_session: Any, test_user: Any, releaseaudit_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    waiver = _waiver()
    answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
    row = _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="ask_user",
        tool_args={
            "question": "Which residual risks do you accept?",
            "options": [waiver["id"]],
        },
        tool_result={
            "answer": answer,
            "answered_by": waiver["author"],
            "answered_at": f"{waiver['date']}T12:00:00Z",
        },
        responses=[
            {
                "user_id": waiver["author"],
                "decision": "approved",
                "comment": answer,
                "timestamp": f"{waiver['date']}T12:00:00Z",
            }
        ],
        approver_comment=answer,
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert len(loaded) == 1
    assert loaded[0].decided_by_ai is False
    assert loaded[0].auto_approved_reason is None
    payload = _release_with_kevs(
        releaseaudit_result,
        [_kev_finding()],
        [_waiver(approval_id=str(row.id))],
        passed=True,
        unwaived=[],
    )
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert not decision.invalid, decision.validation.failures


def test_ai_ask_user_row_cannot_waive(
    db_session: Any, test_user: Any, releaseaudit_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    waiver = _waiver()
    answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
    row = _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="ask_user",
        tool_args={
            "question": "Which residual risks do you accept?",
            "options": [waiver["id"]],
        },
        tool_result={
            "answer": answer,
            "answered_by": waiver["author"],
            "answered_at": f"{waiver['date']}T12:00:00Z",
        },
        responses=[
            {
                "user_id": waiver["author"],
                "decision": "approved",
                "comment": answer,
                "timestamp": f"{waiver['date']}T12:00:00Z",
            }
        ],
        approver_comment=answer,
        decided_by_ai=True,
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert loaded[0].decided_by_ai is True
    payload = _release_with_kevs(
        releaseaudit_result,
        [_kev_finding()],
        [_waiver(approval_id=str(row.id))],
        passed=True,
        unwaived=[],
    )
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid


def test_auto_approved_ask_user_row_cannot_waive(
    db_session: Any, test_user: Any, releaseaudit_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    waiver = _waiver()
    answer = json.dumps([{"id": waiver["id"], "reason": waiver["reason"]}])
    row = _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="ask_user",
        tool_args={
            "question": "Which residual risks do you accept?",
            "options": [waiver["id"]],
        },
        tool_result={
            "answer": answer,
            "answered_by": waiver["author"],
            "answered_at": f"{waiver['date']}T12:00:00Z",
        },
        responses=[
            {
                "user_id": waiver["author"],
                "decision": "approved",
                "comment": answer,
                "timestamp": f"{waiver['date']}T12:00:00Z",
            }
        ],
        approver_comment=answer,
        auto_approved_reason="bypass",
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert loaded[0].auto_approved_reason == "bypass"
    payload = _release_with_kevs(
        releaseaudit_result,
        [_kev_finding()],
        [_waiver(approval_id=str(row.id))],
        passed=True,
        unwaived=[],
    )
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid


def test_ask_user_row_missing_reason_cannot_waive(
    db_session: Any, test_user: Any, releaseaudit_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    waiver = _waiver()
    answer = json.dumps([{"id": waiver["id"]}])
    row = _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="ask_user",
        tool_args={
            "question": "Which residual risks do you accept?",
            "options": [waiver["id"]],
        },
        tool_result={
            "answer": answer,
            "answered_by": waiver["author"],
            "answered_at": f"{waiver['date']}T12:00:00Z",
        },
        responses=[
            {
                "user_id": waiver["author"],
                "decision": "approved",
                "comment": answer,
                "timestamp": f"{waiver['date']}T12:00:00Z",
            }
        ],
        approver_comment=answer,
    )
    loaded = load_platform_approvals(db_session, execution_id)
    payload = _release_with_kevs(
        releaseaudit_result,
        [_kev_finding()],
        [_waiver(approval_id=str(row.id))],
        passed=True,
        unwaived=[],
    )
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid


def test_ask_user_row_differing_reason_cannot_waive(
    db_session: Any, test_user: Any, releaseaudit_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    waiver = _waiver()
    answer = json.dumps(
        [{"id": waiver["id"], "reason": "Stored human reason from the row."}]
    )
    row = _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="ask_user",
        tool_args={
            "question": "Which residual risks do you accept?",
            "options": [waiver["id"]],
        },
        tool_result={
            "answer": answer,
            "answered_by": waiver["author"],
            "answered_at": f"{waiver['date']}T12:00:00Z",
        },
        responses=[
            {
                "user_id": waiver["author"],
                "decision": "approved",
                "comment": answer,
                "timestamp": f"{waiver['date']}T12:00:00Z",
            }
        ],
        approver_comment=answer,
    )
    loaded = load_platform_approvals(db_session, execution_id)
    payload = _release_with_kevs(
        releaseaudit_result,
        [_kev_finding()],
        [_waiver(approval_id=str(row.id), reason="Agent invented a different reason.")],
        passed=True,
        unwaived=[],
    )
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid


def test_human_due_diligence_row_records_decision(
    db_session: Any, test_user: Any, duediligence_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    payload = clone(duediligence_result)
    operation = payload["decision"]["approval_operation"]
    _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="request_approval",
        tool_args={"operation": operation},
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert loaded[0].operation == operation
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert not decision.invalid, decision.validation.failures


def test_ai_due_diligence_row_cannot_record(
    db_session: Any, test_user: Any, duediligence_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    payload = clone(duediligence_result)
    operation = payload["decision"]["approval_operation"]
    _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="request_approval",
        tool_args={"operation": operation},
        decided_by_ai=True,
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert loaded[0].decided_by_ai is True
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid


def test_auto_approved_due_diligence_row_cannot_record(
    db_session: Any, test_user: Any, duediligence_result: dict[str, Any]
) -> None:
    workflow, config = _workflow_and_config(db_session, test_user)
    execution_id = str(uuid.uuid4())
    payload = clone(duediligence_result)
    operation = payload["decision"]["approval_operation"]
    _add_request(
        db_session,
        test_user=test_user,
        workflow=workflow,
        config=config,
        execution_id=execution_id,
        tool_name="request_approval",
        tool_args={"operation": operation},
        auto_approved_reason="native_tool_approvals_off",
    )
    loaded = load_platform_approvals(db_session, execution_id)
    assert loaded[0].auto_approved_reason == "native_tool_approvals_off"
    decision = apply_cra_persist_boundary(
        payload,
        platform_approvals=loaded,
        authority=AUTHORITY_REQUIRED,
    )
    assert decision.invalid
