"""Human authorization and atomic storage for review re-finalization."""

from typing import Any

from preloop.models import crud, models
from sqlalchemy import select
from sqlalchemy.orm import Session


def authorize(db: Session, actor: Any) -> None:
    """Require a current account owner or system administrator acting as a person."""
    if not isinstance(actor, models.User):
        raise PermissionError("A person with owner or administrator role is required")
    key = getattr(actor, "_auth_api_key", None)
    if key is not None and (
        key.requires_machine_authorization
        or key.context_data
        or key.managed_agent_credential is not None
    ):
        raise PermissionError("Agent and execution credentials cannot re-finalize")
    from preloop.utils.permissions import _scoped_held_roles

    account_id, user_id = actor.account_id, actor.id
    account = db.execute(
        select(models.Account)
        .where(models.Account.id == account_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    human = db.execute(
        select(models.User)
        .where(models.User.id == user_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    roles = (
        db.execute(select(models.Role).where(_scoped_held_roles(user_id, account_id)))
        .scalars()
        .all()
    )
    if (
        account is None
        or not account.is_active
        or human is None
        or not human.is_active
        or human.account_id != account.id
        or not (
            account.primary_user_id == human.id
            or any(
                role.is_system_role and role.name in {"owner", "admin"}
                for role in roles
            )
        )
    ):
        raise PermissionError("A person with owner or administrator role is required")


def record(
    db: Session,
    *,
    execution: models.FlowExecution,
    actor: models.User,
    document: dict[str, Any],
    validation: dict[str, Any],
    integrity: dict[str, Any],
    mark: dict[str, Any],
) -> None:
    """Store the outcome and audit in one transaction, leaving metering untouched.

    The caller holds the flow and execution locks and has rechecked the preview.
    An audit failure rolls back the terminal change as well.
    """
    receipt = dict(execution.startup_receipt or {})
    receipt["_refinalization"] = mark
    receipt["_completion"] = {
        **(receipt.get("_completion") or {}),
        "review_integrity": integrity,
        "finalization_state": "settled",
    }
    execution.result = document
    execution.result_schema_validation = validation
    execution.status = "SUCCEEDED"
    execution.failure_category = None
    execution.error_message = None
    execution.startup_receipt = receipt
    try:
        crud.crud_audit_log.log_action(
            db,
            account_id=actor.account_id,
            user_id=actor.id,
            action="execution_refinalize",
            resource_type="flow_execution",
            resource_id=str(execution.id),
            status="success",
            details=mark,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def audit_refusal(
    db: Session,
    *,
    execution: models.FlowExecution,
    actor: models.User,
    result_digest: str,
    reason: str,
) -> None:
    """Audit a refused apply; previews and idempotent repeats write nothing."""
    crud.crud_audit_log.log_action(
        db,
        account_id=actor.account_id,
        user_id=actor.id,
        action="execution_refinalize",
        resource_type="flow_execution",
        resource_id=str(execution.id),
        status="denied",
        details={
            "execution_id": str(execution.id),
            "outcome": "refused",
            "reason": reason,
            "previous_status": execution.status,
            "new_status": execution.status,
            "previous_failure_category": execution.failure_category,
            "new_failure_category": execution.failure_category,
            "previous_result_sha256": result_digest,
            "result_sha256": result_digest,
        },
    )
