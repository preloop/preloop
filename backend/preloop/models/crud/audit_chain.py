"""Transaction-preserving access to the per-account audit chain head."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


def get_state(
    db: Session, *, account_id: Any, for_update: bool = False, create: bool = True
) -> models.AuditChainState | None:
    """Read or initialize a head, optionally locking it until caller commit.

    A missing row cannot be locked with FOR UPDATE. Conflict-ignore insertion
    lets simultaneous first callers share one genesis without aborting either
    transaction or overwriting a head that the winner has already advanced.
    The caller retains ownership of commits, rollbacks, and savepoints.
    """
    stmt = select(models.AuditChainState).where(
        models.AuditChainState.account_id == account_id
    )
    if for_update:
        # Sessions normally disable autoflush. Preserve pending pruning/head
        # changes before refreshing an identity-map copy under the row lock.
        db.flush()
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    state = db.execute(stmt).scalar_one_or_none()
    if state is None and create:
        db.flush()
        db.execute(
            insert(models.AuditChainState)
            .values(
                account_id=account_id,
                last_seq=0,
                last_hash=models.GENESIS_HASH,
                pruned_below_seq=0,
            )
            .on_conflict_do_nothing(index_elements=[models.AuditChainState.account_id])
        )
        # A concurrent initializer may have committed an advanced head while
        # our INSERT waited. Always read its authoritative values and lock it
        # when requested; never reset the head or pruning floor to genesis.
        state = db.execute(stmt.execution_options(populate_existing=True)).scalar_one()
    return state
