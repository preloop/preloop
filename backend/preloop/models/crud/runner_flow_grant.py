"""Durable 'allow this flow on this runner until revoked' grants."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models


def active(
    db: Session, *, runner_id: UUID | str, flow_id: UUID | str, owner_user_id: UUID
) -> Optional[models.RunnerFlowGrant]:
    """Return the unrevoked grant for this runner, flow and owner, if any.

    Args:
        db: Database session.
        runner_id: Runner the flow would run on.
        flow_id: Flow being routed.
        owner_user_id: The runner's registering owner.

    Returns:
        The active grant or ``None``.
    """
    return (
        db.query(models.RunnerFlowGrant)
        .filter(
            models.RunnerFlowGrant.runner_id == UUID(str(runner_id)),
            models.RunnerFlowGrant.flow_id == UUID(str(flow_id)),
            models.RunnerFlowGrant.owner_user_id == owner_user_id,
            models.RunnerFlowGrant.revoked_at.is_(None),
        )
        .first()
    )


def grant(
    db: Session,
    *,
    account_id: UUID,
    owner_user_id: UUID,
    runner_id: UUID,
    flow_id: UUID,
    approval_request_id: Optional[UUID] = None,
) -> tuple[models.RunnerFlowGrant, bool]:
    """Create a grant, or return the existing active one.

    Args:
        db: Database session.
        account_id: Account that owns the runner.
        owner_user_id: Runner owner giving consent.
        runner_id: Runner the grant covers.
        flow_id: Flow the grant covers.
        approval_request_id: Approval the owner granted from, if any.

    Returns:
        ``(grant, created)``.
    """
    existing = active(
        db, runner_id=runner_id, flow_id=flow_id, owner_user_id=owner_user_id
    )
    if existing is not None:
        return existing, False
    row = models.RunnerFlowGrant(
        account_id=account_id,
        owner_user_id=owner_user_id,
        runner_id=runner_id,
        flow_id=flow_id,
        approval_request_id=approval_request_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request created the same active grant.
        db.rollback()
        existing = active(
            db, runner_id=runner_id, flow_id=flow_id, owner_user_id=owner_user_id
        )
        if existing is None:
            raise
        return existing, False
    db.refresh(row)
    return row, True


def list_active(
    db: Session,
    *,
    owner_user_id: UUID,
    runner_id: Optional[UUID] = None,
) -> list[models.RunnerFlowGrant]:
    """List an owner's unrevoked grants, optionally for one runner.

    Args:
        db: Database session.
        owner_user_id: Runner owner.
        runner_id: Restrict to one runner when set.

    Returns:
        Grants, newest first.
    """
    query = db.query(models.RunnerFlowGrant).filter(
        models.RunnerFlowGrant.owner_user_id == owner_user_id,
        models.RunnerFlowGrant.revoked_at.is_(None),
    )
    if runner_id is not None:
        query = query.filter(models.RunnerFlowGrant.runner_id == runner_id)
    return query.order_by(models.RunnerFlowGrant.created_at.desc()).all()


def get_owned(
    db: Session, *, grant_id: UUID, owner_user_id: UUID
) -> Optional[models.RunnerFlowGrant]:
    """Fetch one grant only when it belongs to this owner.

    Args:
        db: Database session.
        grant_id: Grant id.
        owner_user_id: Requesting user.

    Returns:
        The grant or ``None``.
    """
    return (
        db.query(models.RunnerFlowGrant)
        .filter_by(id=grant_id, owner_user_id=owner_user_id)
        .first()
    )


def revoke(
    db: Session, row: models.RunnerFlowGrant, *, revoked_by_user_id: UUID
) -> models.RunnerFlowGrant:
    """Revoke a grant. Later runs of the flow ask the owner again.

    Args:
        db: Database session.
        row: Grant to revoke.
        revoked_by_user_id: User revoking it.

    Returns:
        The revoked grant.
    """
    if row.revoked_at is None:
        row.revoked_at = datetime.utcnow()
        row.revoked_by_user_id = revoked_by_user_id
        db.commit()
        db.refresh(row)
    return row


def names_for(
    db: Session, rows: list[models.RunnerFlowGrant]
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Load runner and flow names for a list of grants in two queries.

    Args:
        db: Database session.
        rows: Grants to describe.

    Returns:
        ``(runner_names, flow_names)`` keyed by id.
    """
    runner_ids = {row.runner_id for row in rows}
    flow_ids = {row.flow_id for row in rows}
    runners = (
        db.query(models.FlowRunner.id, models.FlowRunner.name)
        .filter(models.FlowRunner.id.in_(runner_ids))
        .all()
        if runner_ids
        else []
    )
    flows = (
        db.query(models.Flow.id, models.Flow.name)
        .filter(models.Flow.id.in_(flow_ids))
        .all()
        if flow_ids
        else []
    )
    return (
        {row.id: row.name for row in runners},
        {row.id: row.name for row in flows},
    )
