"""Seed legacy keys whose account differs from their owner's account.

Issuance refuses such keys now, but rows created before that check may still
exist; authentication must keep rejecting them.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_user


def legacy_cross_account_runtime_key(
    db: Session, *, account_id: UUID, user_id: UUID, **kwargs: Any
) -> tuple[models.ApiKey, str]:
    """Issue a runtime key in the owner's account, then rebind it elsewhere.

    Args:
        db: Database session.
        account_id: Account the key is rebound to.
        user_id: Owner whose own account issues the key.
        **kwargs: Passed to ``crud_api_key.create_runtime_key``.

    Returns:
        The rebound key row and its token.
    """
    owner = crud_user.get(db, id=user_id)
    assert owner is not None
    key, token = crud_api_key.create_runtime_key(
        db, account_id=owner.account_id, user_id=user_id, **kwargs
    )
    key.account_id = account_id
    db.flush()
    return key, token
