"""Atomic, audited writes for platform-admin entitlement controls."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from .audit_log import crud_audit_log


def read_snapshot(db: Session) -> dict[str, bool | None]:
    """Read sparse settings; callers may cache this committed snapshot briefly."""
    model = models.EntitlementEnforcementSetting
    return dict(db.execute(select(model.scope, model.enabled)).all())


def set_enforcement(
    db: Session,
    *,
    enabled: bool | None,
    actor: models.User,
    reason: str,
    account_id: UUID | None = None,
) -> None:
    """Serialize a setting and its audit evidence, committing both or neither.

    Global null restores the environment default. Account null restores global
    inheritance. Retaining the null row also gives concurrent editors a stable
    row to lock. The global audit belongs to the platform admin's account.
    """
    if not actor.is_superuser or not actor.is_active:
        raise PermissionError("Only active platform admins may change enforcement")
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise ValueError("A reason of 1 to 1000 characters is required")
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled must be a boolean or null")
    scope = str(account_id) if account_id is not None else "global"
    model = models.EntitlementEnforcementSetting
    try:
        db.execute(
            insert(model)
            .values(scope=scope, account_id=account_id, enabled=None)
            .on_conflict_do_nothing(index_elements=[model.scope])
        )
        row = db.scalars(
            select(model)
            .where(model.scope == scope)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        before = row.enabled
        row.enabled = enabled
        crud_audit_log.log_action(
            db,
            account_id=account_id or actor.account_id,
            user_id=actor.id,
            action="configuration_change",
            resource_type="entitlement_enforcement",
            resource_id=scope,
            status="success",
            details={
                "scope": scope,
                "before": before,
                "after": enabled,
                "reason": reason,
            },
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
