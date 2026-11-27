"""Current identities for device observations, scoped to the owning account."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.account_hooks import filter_viewable, VISIBLE_MANAGED_AGENT


def agent_identities(
    db: Session,
    *,
    account_id: UUID,
    ids: list[UUID],
    user: models.User | None = None,
) -> list[dict[str, str | None]]:
    """Read current enrollment independently of the last collector report."""
    rows = list(
        db.scalars(
            select(models.ManagedAgent).where(
                models.ManagedAgent.account_id == account_id,
                models.ManagedAgent.id.in_(ids),
            )
        )
    )
    if user is not None:
        rows = filter_viewable(db, user, VISIBLE_MANAGED_AGENT, rows)
    return [
        dict(id=str(row.id), name=row.display_name, lifecycle_state=row.lifecycle_state)
        for row in rows
    ]


def observer_names(db: Session, *, account_id: UUID, ids: list[UUID]) -> dict[str, str]:
    """Name authenticated reporters without returning credential material."""
    observed = select(models.DiscoveryObservation.source_ref).where(
        models.DiscoveryObservation.account_id == account_id,
        models.DiscoveryObservation.source_ref.in_(ids),
    )
    keys = db.execute(
        select(models.ApiKey.id, models.ApiKey.name).where(
            models.ApiKey.account_id == account_id, models.ApiKey.id.in_(observed)
        )
    ).all()
    users = db.execute(
        select(models.User.id, models.User.username).where(
            models.User.account_id == account_id, models.User.id.in_(observed)
        )
    ).all()
    return {str(id): name for id, name in [*users, *keys] if name}
