"""Tenant-scoped revision-checked policy persistence; callers own transactions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.extension_policy import PolicyUpdate

from .expected_device import crud_expected_device


def get(
    db: Session, *, account_id: UUID, agent_kind: str
) -> models.ExtensionEnforcementPolicy | None:
    """Read only the selected tenant's policy."""
    model = models.ExtensionEnforcementPolicy
    return db.scalar(
        select(model)
        .where(model.account_id == account_id, model.agent_kind == agent_kind)
        .execution_options(populate_existing=True)
    )


def upsert(
    db: Session,
    *,
    account_id: UUID,
    agent_kind: str,
    payload: PolicyUpdate,
    expected_revision: int,
    user_id: UUID,
) -> models.ExtensionEnforcementPolicy:
    """Serialize first-create and updates, rejecting stale revisions.

    Raises:
        ValueError: If-Match differs from the revision under the account lock.
    """
    crud_expected_device.lock_account(db, account_id=account_id)
    row = get(db, account_id=account_id, agent_kind=agent_kind)
    revision = row.revision if row else 0
    if revision != expected_revision:
        raise ValueError(
            f"Policy revision changed (current {revision}); reload before saving"
        )
    if row is None:
        row = models.ExtensionEnforcementPolicy(
            account_id=account_id, agent_kind=agent_kind
        )
        db.add(row)
    row.marketplaces = [
        m.model_dump(exclude_none=True, by_alias=True) for m in payload.marketplaces
    ]
    row.options = payload.options.model_dump()
    row.revision = revision + 1
    row.updated_by_user_id = user_id
    row.updated_at = datetime.now(UTC)
    db.flush()
    return row
