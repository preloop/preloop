"""Scoped, insert-only persistence for flow change reviews."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


def get_proposal(
    db: Session, *, account_id: UUID, project_id: UUID, flow_id: UUID, digest: str
) -> models.FlowChangeProposal | None:
    """Read the digest only inside its exact target scope."""
    return db.scalar(
        select(models.FlowChangeProposal).filter_by(
            account_id=account_id, project_id=project_id, flow_id=flow_id, digest=digest
        )
    )


def store_proposal(
    db: Session,
    *,
    account_id: UUID,
    project_id: UUID,
    flow_id: UUID,
    digest: str,
    document: str,
) -> bool:
    """Commit once per canonical document, including concurrent submissions."""
    db.execute(
        insert(models.FlowChangeProposal)
        .values(
            account_id=account_id,
            project_id=project_id,
            flow_id=flow_id,
            digest=digest,
            document=document,
        )
        .on_conflict_do_nothing(index_elements=["digest"])
    )
    row = get_proposal(
        db, account_id=account_id, project_id=project_id, flow_id=flow_id, digest=digest
    )
    if row is None or row.document != document:
        return False
    db.commit()
    return True
