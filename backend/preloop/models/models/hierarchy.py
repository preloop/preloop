"""Account tree helpers, and the defaults that keep new rows valid.

``ancestors`` and ``descendants`` are the only sanctioned way to walk the
account tree. They read ``hierarchy_path`` (root-to-node ids, self included)
and ``root_account_id``, never ``parent_account_id``, and nothing in them
depends on how deep the tree is: the depth limit is the
``ck_account_hierarchy_depth_max`` CHECK on ``account`` and nothing else.
Relaxing that CHECK needs one schema addition, a trigger that ties a child's
path prefix to its parent's path (see the note next to the CHECK).

The ``before_flush`` hook fills the hierarchy columns that the schema makes
NOT NULL, so existing code that creates accounts and users keeps working
without knowing about the tree or about persons:

* a new account without a parent becomes a root (``root_account_id = id``,
  ``hierarchy_path = [id]``, depth 0). A child must be placed with
  ``place_under``;
* a new user row without a person gets a person of its own. The person is
  verified only when the row's email is verified and no verified person holds
  that address yet; otherwise it is provisional. The hook never links a new
  row to an existing person: merging memberships is a decision for the
  membership service, which has to prove the address first;
* a user row whose ``email_verified`` turns true verifies its provisional
  person, on the same terms: only when no verified person holds the address
  yet. If one does, the row stays provisional for the membership service to
  merge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from .account import Account
from .person import Person, normalize_email
from .user import User


@dataclass(frozen=True)
class HierarchyPosition:
    """Where an account sits in its tree."""

    root_account_id: uuid.UUID
    hierarchy_path: List[uuid.UUID]
    hierarchy_depth: int


def root_position(account_id: uuid.UUID) -> HierarchyPosition:
    """Position of a root account."""
    return HierarchyPosition(
        root_account_id=account_id,
        hierarchy_path=[account_id],
        hierarchy_depth=0,
    )


def child_position(parent: Account, child_id: uuid.UUID) -> HierarchyPosition:
    """Position of ``child_id`` directly under ``parent``.

    Does not check the depth limit: the database CHECK does, so that relaxing
    it is a schema change and nothing else.
    """
    parent_path = list(parent.hierarchy_path or [])
    if not parent_path or parent_path[-1] != parent.id:
        raise ValueError(f"account {parent.id} has no hierarchy path yet")
    return HierarchyPosition(
        root_account_id=parent.root_account_id,
        hierarchy_path=[*parent_path, child_id],
        hierarchy_depth=len(parent_path),
    )


def place_under(child: Account, parent: Account) -> Account:
    """Set ``child``'s parent and hierarchy columns for a new subaccount."""
    if child.id is None:
        child.id = uuid.uuid4()
    position = child_position(parent, child.id)
    child.parent_account_id = parent.id
    child.root_account_id = position.root_account_id
    child.hierarchy_path = position.hierarchy_path
    child.hierarchy_depth = position.hierarchy_depth
    return child


def ancestors(db: Session, account: Account) -> List[Account]:
    """Accounts above ``account``, root first. Empty for a root."""
    ids = list(account.hierarchy_path or [])[:-1]
    if not ids:
        return []
    return list(
        db.scalars(
            select(Account).where(Account.id.in_(ids)).order_by(Account.hierarchy_depth)
        )
    )


def descendants(db: Session, account: Account) -> List[Account]:
    """Accounts below ``account`` at any depth, shallowest first."""
    return list(
        db.scalars(
            select(Account)
            .where(
                Account.root_account_id == account.root_account_id,
                Account.hierarchy_path.contains([account.id]),
                Account.id != account.id,
            )
            .order_by(Account.hierarchy_depth, Account.id)
        )
    )


def _fill_account(account: Account) -> None:
    if account.hierarchy_path is not None or account.root_account_id is not None:
        return
    if account.parent_account_id is not None:
        raise ValueError(
            "a subaccount needs its hierarchy columns: create it with place_under()"
        )
    if account.id is None:
        account.id = uuid.uuid4()
    position = root_position(account.id)
    account.root_account_id = position.root_account_id
    account.hierarchy_path = position.hierarchy_path
    account.hierarchy_depth = position.hierarchy_depth


def _verified_person_exists(session: Session, email: str) -> bool:
    with session.no_autoflush:
        return (
            session.scalar(
                select(Person.id)
                .where(
                    Person.email_normalized == email,
                    Person.email_verified_at.is_not(None),
                )
                .limit(1)
            )
            is not None
        )


def _fill_person(session: Session, user: User, claimed: set[str]) -> None:
    if user.person_id is not None or user.person is not None:
        return
    email = normalize_email(user.email)
    verified = (
        bool(user.email_verified)
        and email not in claimed
        and not _verified_person_exists(session, email)
    )
    if verified:
        claimed.add(email)
    person = Person(
        id=uuid.uuid4(),
        email_normalized=email,
        email_verified_at=datetime.now(timezone.utc) if verified else None,
    )
    person.primary_user = user
    person.last_active_user = user
    user.person = person
    session.add(person)


def _verify_person(session: Session, user: User, claimed: set[str]) -> None:
    if True not in inspect(user).attrs.email_verified.history.added:
        return
    with session.no_autoflush:
        person = user.person
    if person is None or person.email_verified_at is not None:
        return
    email = normalize_email(user.email)
    if email in claimed or _verified_person_exists(session, email):
        return
    claimed.add(email)
    # A provisional person holds exactly this one row, so its address follows
    # the row's current email.
    person.email_normalized = email
    person.email_verified_at = datetime.now(timezone.utc)


@event.listens_for(Session, "before_flush")
def _fill_hierarchy_defaults(
    session: Session, flush_context: Any, instances: Any
) -> None:
    """Give new accounts and users the hierarchy columns they must carry."""
    claimed: set[str] = set()
    # Rows that already exist claim their address before new rows do, so the
    # older, first-verified membership keeps the verified person.
    for obj in list(session.dirty):
        if isinstance(obj, User):
            _verify_person(session, obj, claimed)
    for obj in list(session.new):
        if isinstance(obj, Account):
            _fill_account(obj)
        elif isinstance(obj, User):
            _fill_person(session, obj, claimed)
