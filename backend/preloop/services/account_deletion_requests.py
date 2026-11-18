"""Account deletion requests: who may ask, how a request is proven, who hears.

A deletion request reaches the instance operators (``notify_admins``), who
then delete the account by hand. Before it does, the request has to be
proven, in one of two ways:

* **Signed-in session.** The caller is authenticated; the account, username
  and address come from the session, never the request body. Only an owner of
  the account, or the single member of a personal account, may ask.
* **Emailed link.** A signed-out caller names an address. For every identity
  that holds that address *verified*, a one-time link goes to it; the
  operators hear only when the link is followed. Unknown and unverified
  addresses get the same answer and nothing is sent.

Each confirmed request writes one audit row (``account_deletion_requested``)
in the account it names, and the operator notification says which proof it
carries.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_audit_log,
    crud_person,
    crud_user,
    crud_user_role,
)
from preloop.models.models.user import User
from preloop.services import rate_limit
from preloop.services.rate_limit import Rule
from preloop.utils.tokens import (
    ACCOUNT_DELETION_TOKEN_EXPIRE_MINUTES,
    AccountDeletionClaims,
)

logger = logging.getLogger(__name__)

#: Longest reason for leaving the API accepts.
REASON_MAX_LENGTH = 1000

#: How a request was proven; stored in the audit row and shown to operators.
CONFIRMED_BY_SESSION = "signed_in_session"
CONFIRMED_BY_EMAIL_LINK = "emailed_link"
_CONFIRMATION_LABELS = {
    CONFIRMED_BY_SESSION: "signed-in session",
    CONFIRMED_BY_EMAIL_LINK: "link emailed to the account's verified address",
}

AUDIT_ACTION = "account_deletion_requested"

#: Records each used confirmation link (keyed by its ``jti``). The bucket
#: outlives the link, so a link cannot be replayed before it expires.
DELETION_LINK_CONSUMED = Rule(
    "account.deletion_link.consumed",
    1,
    max(ACCOUNT_DELETION_TOKEN_EXPIRE_MINUTES, 1) * 60 * 2,
)


def may_request_deletion(db: Session, user: User) -> bool:
    """Whether ``user`` may ask for their account to be deleted.

    Args:
        db: Database session.
        user: An active account member.

    Returns:
        True for a holder of the system ``owner`` role in the user's account,
        and for the only member of the account.
    """
    if not user.is_active:
        return False
    for role in crud_user_role.get_user_roles(db, user_id=user.id):
        if role.account_id is not None and role.account_id != user.account_id:
            continue
        if role.name == "owner" and role.is_system_role:
            return True
    return crud_user.count_by_account(db, account_id=str(user.account_id)) == 1


def deletable_memberships(db: Session, person_id: Any) -> list[User]:
    """Memberships of one identity for which a deletion request is allowed.

    Args:
        db: Database session.
        person_id: The identity's id.

    Returns:
        Active memberships where :func:`may_request_deletion` holds.
    """
    return [
        user
        for user in crud_person.list_memberships(db, person_id=person_id)
        if may_request_deletion(db, user)
    ]


def verified_people_for_address(db: Session, email: str) -> list[Any]:
    """Identities that hold ``email`` verified and may receive mail.

    Args:
        db: Database session.
        email: The address a signed-out request names.

    Returns:
        People whose address is verified and who are not quarantined.
    """
    return [
        person
        for person in crud_person.list_by_email(db, email=email)
        if person.email_verified_at is not None
        and person.identity_status != "quarantined"
    ]


def link_still_valid(db: Session, claims: AccountDeletionClaims) -> Optional[Any]:
    """Resolve the identity a confirmation link names, if it still holds.

    Args:
        db: Database session.
        claims: Verified token claims.

    Returns:
        The person, or None when the identity is gone, changed its address or
        credentials since the link was sent, or is no longer verified.
    """
    from preloop.models.models.person import normalize_email

    person = crud_person.get(db, id=claims.person_id)
    if person is None:
        return None
    if (
        person.email_verified_at is None
        or person.identity_status == "quarantined"
        or person.auth_generation != claims.auth_generation
        or person.email_normalized != normalize_email(claims.email)
    ):
        return None
    return person


def consume_link(db: Session, claims: AccountDeletionClaims) -> bool:
    """Record a confirmation link as used, in the caller's transaction.

    Args:
        db: Session; the caller commits (or rolls back to give the link back).
        claims: Verified token claims.

    Returns:
        True the first time a link is used, False on every later use.
    """
    now = datetime.now(UTC).timestamp()
    count = rate_limit.transactional_count(
        db,
        DELETION_LINK_CONSUMED,
        claims.jti,
        now=now,
        started_at=float(claims.issued_at),
        increment=True,
    )
    return count == 1


@dataclass(frozen=True)
class DeletionNotice:
    """One proven request, ready for the operators."""

    subject: str
    text: str
    html: str


def _chat_safe(value: str) -> str:
    """Escape ``&``, ``<`` and ``>`` so chat webhooks render text, not markup."""
    return html.escape(value, quote=False)


def build_notice(
    db: Session,
    *,
    user: User,
    confirmed_by: str,
    reason: Optional[str],
) -> DeletionNotice:
    """Compose the operator notification for one proven request.

    Every identifying field comes from the database. The reason is the only
    requester text, and it is escaped for both email and chat.

    Args:
        db: Database session.
        user: The membership whose account is to be deleted.
        confirmed_by: ``CONFIRMED_BY_SESSION`` or ``CONFIRMED_BY_EMAIL_LINK``.
        reason: Optional reason for leaving.

    Returns:
        Subject, plain text and HTML bodies.
    """
    account = crud_account.get(db, id=user.account_id)
    org_name = getattr(account, "organization_name", None) or ""
    label = _CONFIRMATION_LABELS[confirmed_by]
    fields = [
        ("Confirmed by", label),
        ("User", user.username),
        ("Email", user.email or ""),
        ("Account ID", str(user.account_id)),
    ]
    if org_name:
        fields.append(("Organization", org_name))
    lines = [f"{name}: {_chat_safe(value)}" for name, value in fields]
    html_parts = [
        f"<p><strong>{name}:</strong> {html.escape(value)}</p>"
        for name, value in fields
    ]
    if reason:
        lines.append(f"\nReason: {_chat_safe(reason)}")
        html_parts.append(f"<p><strong>Reason:</strong> {html.escape(reason)}</p>")
    subject = f"Account Deletion Request (confirmed by {label}): {user.username}"
    return DeletionNotice(
        subject=subject,
        text="\n".join(lines),
        html="<h2>Account Deletion Request</h2>\n" + "\n".join(html_parts),
    )


def record_request(
    db: Session,
    *,
    user: User,
    confirmed_by: str,
    reason: Optional[str],
    ip_address: Optional[str],
    user_agent: Optional[str],
) -> None:
    """Write the audit row for one proven request (flush only).

    Args:
        db: Session; the caller commits.
        user: The membership whose account is to be deleted.
        confirmed_by: How the request was proven.
        reason: Optional reason, stored with the row.
        ip_address: Client address of the confirming request.
        user_agent: User agent of the confirming request.
    """
    crud_audit_log.log_action(
        db,
        account_id=user.account_id,
        user_id=user.id,
        action=AUDIT_ACTION,
        resource_type="account",
        resource_id=str(user.account_id),
        status="success",
        ip_address=ip_address,
        user_agent=user_agent,
        details={"confirmed_by": confirmed_by, "reason": reason or None},
        commit=False,
    )
