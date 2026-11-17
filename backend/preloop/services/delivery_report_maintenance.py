"""Operator maintenance for Delivery Report flows the system created.

Earlier releases installed a Delivery Report flow in every account that
connected a tracker. Accounts now get one only when a user adds it or an
operator enables it here. Both commands print what they would do and change
nothing unless ``--apply`` is given::

    python -m preloop.services.delivery_report_maintenance remove-unclaimed
    python -m preloop.services.delivery_report_maintenance remove-unclaimed \
        --account-id <account-id> --apply
    python -m preloop.services.delivery_report_maintenance enable \
        --account-id <account-id> --apply

``remove-unclaimed`` deletes a Delivery Report flow only when no user created
it, no user edited it (enable/disable toggles do not count) and none of its
runs ever succeeded. Deletion goes through the API's delete path: running
executions and restricting references block it, executions cascade, and the
deletion is audited as a system action.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log, crud_flow, crud_flow_execution
from preloop.services.delivery_report import is_delivery_report_flow

logger = logging.getLogger(__name__)

# Audit action recorded when an operator enables the report for an account.
OPERATOR_ENABLED_ACTION = "operator_enabled"


@dataclass
class FlowAction:
    """What the command did, or would do, for one flow."""

    flow_id: str
    account_id: str
    name: str
    is_enabled: bool
    action: str
    reason: str = ""


@dataclass
class MaintenanceSummary:
    """The command's printed result."""

    dry_run: bool
    flows: List[FlowAction] = field(default_factory=list)


def _keep_reason(db: Session, flow: Any) -> Optional[str]:
    """Why a system-created report flow belongs to its account, if it does."""
    if getattr(flow, "prompt_customized", False) or getattr(
        flow, "tools_customized", False
    ):
        return "customized by a user"
    if crud_audit_log.flow_changed_by_user(
        db, account_id=flow.account_id, flow_id=flow.id
    ):
        return "edited by a user"
    if crud_audit_log.flow_has_config_action(
        db,
        account_id=flow.account_id,
        flow_id=flow.id,
        action=OPERATOR_ENABLED_ACTION,
    ):
        return "enabled by an operator"
    if crud_flow_execution.flow_has_succeeded(db, flow_id=flow.id):
        return "has a successful run"
    return None


def remove_unclaimed_report_flows(
    db: Session, *, account_id: Optional[UUID] = None, dry_run: bool = True
) -> MaintenanceSummary:
    """Remove system-created Delivery Report flows nobody adopted.

    Args:
        db: Database session.
        account_id: Limit to one account; every account when omitted.
        dry_run: Report what would be removed and change nothing.

    Returns:
        One entry per system-created Delivery Report flow: removed (or
        ``would_remove``), kept with the reason, or refused by the delete path.
    """
    from preloop.services.flow_deletion import (
        FlowDeleteRefusedError,
        delete_flow_checked,
    )

    summary = MaintenanceSummary(dry_run=dry_run)
    for flow in crud_flow.get_system_created(db, account_id=account_id):
        if not is_delivery_report_flow(flow):
            continue
        entry = FlowAction(
            flow_id=str(flow.id),
            account_id=str(flow.account_id),
            name=str(flow.name),
            is_enabled=bool(flow.is_enabled),
            action="keep",
        )
        summary.flows.append(entry)
        reason = _keep_reason(db, flow)
        if reason:
            entry.reason = reason
            continue
        if dry_run:
            entry.action = "would_remove"
            continue
        actor = SimpleNamespace(id=None, account_id=flow.account_id)
        try:
            delete_flow_checked(db, flow=flow, account_id=flow.account_id, actor=actor)
        except FlowDeleteRefusedError as refused:
            entry.action = "refused"
            entry.reason = refused.detail
            continue
        entry.action = "removed"
    return summary


def enable_for_account(
    db: Session, *, account_id: UUID, dry_run: bool = True
) -> MaintenanceSummary:
    """Install the default Delivery Report flow in one account, explicitly.

    Args:
        db: Database session.
        account_id: The account an operator chose.
        dry_run: Report what would be installed and change nothing.

    Returns:
        The existing or new flow (``exists``, ``would_create``, ``created``).
    """
    from preloop.services.delivery_report_activation import (
        ensure_default_report_flow,
    )

    summary = MaintenanceSummary(dry_run=dry_run)
    existing = next(
        (
            flow
            for flow in crud_flow.get_by_account(db, account_id=account_id, limit=1000)
            if is_delivery_report_flow(flow)
        ),
        None,
    )
    if existing is None and dry_run:
        summary.flows.append(
            FlowAction(
                flow_id="",
                account_id=str(account_id),
                name="Delivery Report",
                is_enabled=True,
                action="would_create",
            )
        )
        return summary
    flow = existing or ensure_default_report_flow(db, account_id=account_id)
    if flow is None:
        summary.flows.append(
            FlowAction(
                flow_id="",
                account_id=str(account_id),
                name="Delivery Report",
                is_enabled=False,
                action="unavailable",
                reason="the Delivery Report preset is not installed",
            )
        )
        return summary
    if existing is None:
        db.commit()
        # The operator's explicit enable adopts the flow: remove-unclaimed
        # keeps flows with this entry. Written directly so it does not depend
        # on the audit service being enabled.
        crud_audit_log.log_action(
            db=db,
            account_id=account_id,
            user_id=None,
            action="configuration_change",
            resource_type="configuration",
            resource_id="flow",
            status="success",
            details={
                "config_type": "flow",
                "action": OPERATOR_ENABLED_ACTION,
                "new_value": {"id": str(flow.id), "name": flow.name},
            },
        )
        db.commit()
    summary.flows.append(
        FlowAction(
            flow_id=str(flow.id),
            account_id=str(account_id),
            name=str(flow.name),
            is_enabled=bool(flow.is_enabled),
            action="exists" if existing is not None else "created",
        )
    )
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    """Command line entry point for operators.

    Args:
        argv: Arguments, defaulting to the process arguments.

    Returns:
        Process exit status: 0, or 1 when a delete was refused.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    remove = commands.add_parser(
        "remove-unclaimed",
        help="Delete system-created report flows no user adopted.",
    )
    remove.add_argument("--account-id", type=UUID, default=None)
    remove.add_argument("--apply", action="store_true", help="Make the change.")
    enable = commands.add_parser(
        "enable", help="Install the Delivery Report flow in one account."
    )
    enable.add_argument("--account-id", type=UUID, required=True)
    enable.add_argument("--apply", action="store_true", help="Make the change.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        if args.command == "enable":
            summary = enable_for_account(
                db, account_id=args.account_id, dry_run=not args.apply
            )
        else:
            summary = remove_unclaimed_report_flows(
                db, account_id=args.account_id, dry_run=not args.apply
            )
    finally:
        db.close()
    print(json.dumps(asdict(summary), indent=2))
    return 1 if any(flow.action == "refused" for flow in summary.flows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
