#!/usr/bin/env python3
"""Turn off passkey-only sign-in for a user who lost their passkey.

Usage (self-hosted, inside the API container):

    python scripts/reset_passkey_only.py --email user@example.com \
        --reason "lost laptop, identity confirmed by phone"

For every membership row holding the address this sets ``passkey_only`` to
false, ends all of the user's non-SSO sessions and writes one
``auth.passkey_only.reset`` audit event carrying the reason. The user then
signs in with their password (or resets it) and registers a new passkey.

There is deliberately no self-service recovery by email: that would reduce
the account to one factor again. Confirm the person's identity out of band
before running this.

Exit status: 0 on success, 1 when no user holds the address.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

# Import preloop from THIS repo's backend/, ahead of any installed package
# (an editable install may point at a different checkout).
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from sqlalchemy.orm import Session  # noqa: E402

from preloop.models.crud import crud_user  # noqa: E402
from preloop.services.audit.service import AuditActionType  # noqa: E402
from preloop.services.auth_audit import record_auth_event  # noqa: E402


def reset_passkey_only(db: Session, *, email: str, reason: str) -> int:
    """Clear passkey-only sign-in for every membership holding ``email``.

    Args:
        db: Database session; committed here.
        email: Address of the user (matched exactly, then lower-cased).
        reason: Operator-supplied reason, stored in the audit event.

    Returns:
        The number of membership rows reset; 0 when no user holds the address.
    """
    users = crud_user.list_by_email(db, email=email)
    if not users and email != email.lower():
        users = crud_user.list_by_email(db, email=email.lower())
    for found in users:
        user = crud_user.lock(db, user_id=found.id)
        if user is None:
            continue
        crud_user.set_passkey_only(
            db, user=user, enabled=False, end_sessions=True, commit=False
        )
        record_auth_event(
            db,
            action=AuditActionType.PASSKEY_ONLY_RESET,
            user=user,
            resource_type="user",
            resource_id=user.id,
            details={"reason": reason, "enabled": False},
            commit=False,
        )
    db.commit()
    return len(users)


def _default_sessions() -> Generator[Session, None, None]:
    from preloop.models.db.session import get_db_session

    return get_db_session()


def main(
    argv: Sequence[str] | None = None,
    session_factory: Callable[[], Generator[Session, None, None]] = _default_sessions,
) -> int:
    """Parse arguments, reset the user and print the outcome.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
        session_factory: Yields a database session (tests supply their own).

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True, help="The user's email address")
    parser.add_argument(
        "--reason", required=True, help="Why the reset is needed (audited)"
    )
    args = parser.parse_args(argv)
    reason = args.reason.strip()
    if not reason:
        parser.error("--reason must not be empty")

    sessions = session_factory()
    db = next(sessions)
    try:
        count = reset_passkey_only(db, email=args.email.strip(), reason=reason)
    finally:
        sessions.close()
    if count == 0:
        print(f"No user found with email {args.email}", file=sys.stderr)
        return 1
    print(
        f"Passkey-only sign-in turned off for {count} membership(s) of "
        f"{args.email}; their non-SSO sessions have ended."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
