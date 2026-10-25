"""Provision editable note rules and caller-bound human approval workflows."""

import logging
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models import models

SEED_KEY = "send_note_lineage_policy_v1"
CALLER_APPROVER = "note_caller"
logger = logging.getLogger(__name__)


def _note_config(db: Session, account_id: Any) -> models.ToolConfiguration | None:
    """Read the seed marker, refreshing a row cached before the account lock."""
    return (
        db.query(models.ToolConfiguration)
        .filter_by(
            account_id=account_id,
            tool_name="send_note",
            tool_source="builtin",
            managed_agent_id=None,
        )
        .populate_existing()
        .first()
    )


def seed_note_policy(db: Session, *, account_id: Any) -> bool:
    """Seed once, preserving existing rules and subsequent user edits."""
    config = _note_config(db, account_id)
    if config is not None and (config.custom_config or {}).get(SEED_KEY):
        return False
    account = db.query(models.Account).filter_by(id=account_id).with_for_update().one()
    config = _note_config(db, account_id)
    if config is not None and (config.custom_config or {}).get(SEED_KEY):
        return False
    if config is None:
        config = models.ToolConfiguration(
            account_id=account.id,
            tool_name="send_note",
            tool_source="builtin",
            is_enabled=False,
        )
        db.add(config)
        db.flush()
    workflow = (
        db.query(models.ApprovalWorkflow)
        .filter_by(
            account_id=account.id,
            name="Note sender approval",
        )
        .first()
    )
    if (
        workflow is None
        or (workflow.approval_config or {}).get("approver") != CALLER_APPROVER
    ):
        workflow = models.ApprovalWorkflow(
            account_id=account.id,
            name=(
                "Note sender approval"
                if workflow is None
                else f"Note sender approval {config.id}"
            ),
            description="Ask the human who owns the sending agent before contacting another run.",
            approval_type="standard",
            approval_mode="standard",
            approval_config={"approver": CALLER_APPROVER},
            async_approval_enabled=True,
            approvals_required=1,
        )
        db.add(workflow)
        db.flush()
    rules = [
        (
            "allow",
            'args.note_target_relation in ["descendant", "self"]',
            "Allow notes to this run and the workers it started",
            None,
        ),
        (
            "require_approval",
            '!(args.note_target_relation in ["descendant", "self"]) && args.caller_user_can_steer_target',
            "Ask the sender's human before contacting another run they can control",
            workflow.id,
        ),
        ("deny", "true", "Deny notes to runs the sender's human cannot control", None),
    ]
    # Existing account rules keep their priority and continue to win.
    priority = max((r.priority for r in config.access_rules), default=0) + 1000
    for offset, (action, expression, description, workflow_id) in enumerate(rules):
        db.add(
            models.ToolAccessRule(
                account_id=account.id,
                tool_configuration_id=config.id,
                action=action,
                condition_type="cel",
                condition_expression=expression,
                description=description,
                priority=priority + offset,
                approval_workflow_id=workflow_id,
                is_enabled=True,
            )
        )
    config.custom_config = {**(config.custom_config or {}), SEED_KEY: True}
    db.flush()
    return True


def seed_all_note_policies(db: Session) -> int:
    """Backfill independently; failed accounts are logged and retried next boot."""
    account_ids = [row.id for row in db.query(models.Account.id).all()]
    count = 0
    for account_id in account_ids:
        try:
            seeded = seed_note_policy(db, account_id=account_id)
            db.commit()
            count += int(seeded)
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Failed to seed note policy for account %s", account_id)
    return count


def caller_approval_workflow(
    db: Session,
    *,
    account_id: Any,
    workflow_id: Any,
    user_id: Any,
) -> Any:
    """Bind a caller-template workflow to one human using an ordinary workflow row.

    Separate rows keep notification recipients and decision permissions identical
    without ever mutating a shared template during concurrent calls.
    """
    workflow = (
        db.query(models.ApprovalWorkflow)
        .filter_by(
            id=workflow_id,
            account_id=account_id,
        )
        .first()
    )
    if (
        workflow is None
        or (workflow.approval_config or {}).get("approver") != CALLER_APPROVER
    ):
        return workflow_id
    if not user_id:
        return None
    user = (
        db.query(models.User)
        .filter_by(id=UUID(str(user_id)), account_id=account_id, is_active=True)
        .first()
    )
    if user is None:
        return None
    # Serialize creation for this account and deterministic workflow identity.
    db.query(models.Account).filter_by(id=account_id).with_for_update().one()
    bound_id = uuid5(NAMESPACE_URL, f"preloop:note-approval:{workflow.id}:{user.id}")
    bound = (
        db.query(models.ApprovalWorkflow)
        .filter_by(id=bound_id, account_id=account_id)
        .first()
    )
    if bound is None:
        bound = models.ApprovalWorkflow(
            id=bound_id,
            account_id=account_id,
            name=f"Note sender approval {user.id}",
            description="Human approval for notes sent by this user's agents.",
            approval_type="standard",
            approval_mode="standard",
            approver_user_ids=[user.id],
            approver_team_ids=[],
            approvals_required=1,
            approval_config={"note_caller_user_id": str(user.id)},
            require_reason=workflow.require_reason,
            timeout_seconds=workflow.timeout_seconds,
            async_approval_enabled=workflow.async_approval_enabled,
        )
        db.add(bound)
        db.commit()
    else:
        changed = False
        for field in ("timeout_seconds", "async_approval_enabled", "require_reason"):
            value = getattr(workflow, field)
            if getattr(bound, field) != value:
                setattr(bound, field, value)
                changed = True
        if changed:
            db.commit()
    return bound.id
