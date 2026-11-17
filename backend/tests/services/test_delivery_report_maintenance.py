"""Operator cleanup removes only report flows no user adopted."""

from typing import Any, Optional
from unittest.mock import patch
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import delivery_report_maintenance as maintenance


def _flow(
    db: Session,
    account_id: UUID,
    *,
    name: str,
    created_by: Optional[UUID] = None,
    report: bool = True,
    runs: tuple[str, ...] = ("FAILED",),
) -> models.Flow:
    """A scheduled flow with the given run statuses."""
    flow = crud_flow.create(
        db,
        account_id=account_id,
        flow_in=FlowCreate(
            name=name,
            account_id=account_id,
            agent_type="codex",
            agent_config={"report_kind": "delivery_report"} if report else {},
            prompt_template="Report",
            trigger_event_source="schedule",
            trigger_event_types=["schedule"],
        ),
        created_by_user_id=created_by,
    )
    for status in runs:
        crud_flow_execution.create(
            db, obj_in=FlowExecutionCreate(flow_id=flow.id, status=status)
        )
    db.flush()
    return flow


def _audit(db: Session, flow: models.Flow, user_id: UUID, action: str) -> None:
    crud_audit_log.log_action(
        db=db,
        account_id=flow.account_id,
        user_id=user_id,
        action="configuration_change",
        resource_type="configuration",
        resource_id="flow",
        status="success",
        details={
            "config_type": "flow",
            "action": action,
            "new_value": {"id": str(flow.id), "name": flow.name},
        },
    )


def _actions(summary: Any) -> dict[str, tuple[str, str]]:
    return {entry.name: (entry.action, entry.reason) for entry in summary.flows}


def test_dry_run_lists_only_unadopted_report_flows(
    db_session: Session, test_user: Any
) -> None:
    """Dry run is the default and changes nothing."""
    account = test_user.account_id
    unclaimed = _flow(db_session, account, name="system-failed", runs=("FAILED",) * 3)
    toggled = _flow(db_session, account, name="system-toggled")
    _audit(db_session, toggled, test_user.id, "disabled")
    edited = _flow(db_session, account, name="system-edited")
    _audit(db_session, edited, test_user.id, "updated")
    _flow(db_session, account, name="system-succeeded", runs=("FAILED", "SUCCEEDED"))
    _flow(db_session, account, name="user-created", created_by=test_user.id)
    _flow(db_session, account, name="system-other", report=False)

    summary = maintenance.remove_unclaimed_report_flows(db_session, account_id=account)

    assert summary.dry_run is True
    assert _actions(summary) == {
        "system-failed": ("would_remove", ""),
        "system-toggled": ("would_remove", ""),
        "system-edited": ("keep", "edited by a user"),
        "system-succeeded": ("keep", "has a successful run"),
    }
    assert crud_flow.get(db_session, id=unclaimed.id) is not None


def test_apply_uses_the_delete_path_and_its_blockers(
    db_session: Session, test_user: Any
) -> None:
    """Executions cascade, deletion is audited, a running run blocks it."""
    account = test_user.account_id
    unclaimed = _flow(db_session, account, name="system-failed", runs=("FAILED",) * 3)
    running = _flow(db_session, account, name="system-running", runs=("RUNNING",))
    other_account_flow = unclaimed.id

    with patch("preloop.services.flow_deletion.log_config_change") as audit:
        summary = maintenance.remove_unclaimed_report_flows(
            db_session, account_id=account, dry_run=False
        )

    actions = _actions(summary)
    assert actions["system-failed"] == ("removed", "")
    assert actions["system-running"][0] == "refused"
    assert "active execution" in actions["system-running"][1]
    assert crud_flow.get(db_session, id=other_account_flow) is None
    assert crud_flow.get(db_session, id=running.id) is not None
    assert (
        db_session.query(models.FlowExecution)
        .filter(models.FlowExecution.flow_id == other_account_flow)
        .count()
        == 0
    )
    audit.assert_called_once()
    assert audit.call_args.kwargs["action"] == "deleted"
    assert audit.call_args.kwargs["user"].id is None
    assert str(audit.call_args.kwargs["user"].account_id) == str(account)


def test_account_filter_leaves_other_accounts_alone(
    db_session: Session, test_user: Any
) -> None:
    """A run scoped to one account never touches another account's flow."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other synthetic account"}
    )
    mine = _flow(db_session, test_user.account_id, name="mine")
    theirs = _flow(db_session, other.id, name="theirs")
    maintenance.remove_unclaimed_report_flows(
        db_session, account_id=test_user.account_id, dry_run=False
    )
    assert crud_flow.get(db_session, id=mine.id) is None
    assert crud_flow.get(db_session, id=theirs.id) is not None


def test_enable_installs_one_flow_only_when_applied(
    db_session: Session, test_user: Any
) -> None:
    """Operator enablement is explicit, per account, and idempotent."""
    account = test_user.account_id
    with patch(
        "preloop.api.endpoints.flows._resolve_clone_model_binding", return_value=None
    ):
        planned = maintenance.enable_for_account(db_session, account_id=account)
        assert [f.action for f in planned.flows] == ["would_create"]
        created = maintenance.enable_for_account(
            db_session, account_id=account, dry_run=False
        )
        again = maintenance.enable_for_account(
            db_session, account_id=account, dry_run=False
        )
    assert [f.action for f in created.flows] == ["created"]
    assert [f.action for f in again.flows] == ["exists"]
    assert created.flows[0].flow_id == again.flows[0].flow_id


def test_main_defaults_to_dry_run(capsys: Any) -> None:
    """The command prints JSON and does not apply without --apply."""
    with patch.object(
        maintenance,
        "remove_unclaimed_report_flows",
        return_value=maintenance.MaintenanceSummary(dry_run=True),
    ) as remove:
        assert maintenance.main(["remove-unclaimed"]) == 0
    assert remove.call_args.kwargs["dry_run"] is True
    assert '"dry_run": true' in capsys.readouterr().out


def test_operator_enabled_flow_survives_remove_unclaimed(
    db_session: Session, test_user: Any
) -> None:
    """An explicit operator enable adopts the flow it creates."""
    account = test_user.account_id
    with patch(
        "preloop.api.endpoints.flows._resolve_clone_model_binding", return_value=None
    ):
        created = maintenance.enable_for_account(
            db_session, account_id=account, dry_run=False
        )
    summary = maintenance.remove_unclaimed_report_flows(
        db_session, account_id=account, dry_run=False
    )
    assert [(f.action, f.reason) for f in summary.flows] == [
        ("keep", "enabled by an operator")
    ]
    assert crud_flow.get(db_session, id=UUID(created.flows[0].flow_id)) is not None
