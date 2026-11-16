"""Persistence for the account-level policy defaults block."""

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import crud_account


def replace(db: Session, account_id: Any, defaults: dict[str, Any]) -> None:
    """Replace defaults in the caller's transaction, preserving other metadata."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        raise ValueError("Account not found")
    account.meta_data = {**(account.meta_data or {}), "policy_defaults": defaults}
    flag_modified(account, "meta_data")
    db.add(account)
    db.flush()
