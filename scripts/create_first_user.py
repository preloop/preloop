#!/usr/bin/env python3
"""Create the first user of a fresh Preloop instance.

Self-hosted installs that turn public signup off (REGISTRATION_ENABLED=false)
still need one account to start from. This creates it directly against the
database, so an instance can be locked down from the first second it is
reachable rather than racing a stranger to the signup form.

The user is created exactly as the signup endpoint would create it — own
account, owner role, default approval workflow — except that the email is
marked verified and no verification mail is sent, because a fresh install has
no SMTP configured yet.

Refuses to run once any user exists: this bootstraps an empty instance, it does
not add users to a running one (use invitations for that).

Usage:
    python scripts/create_first_user.py --username admin --email a@b.c --password ...
    PRELOOP_ADMIN_PASSWORD=... python scripts/create_first_user.py -u admin -e a@b.c

Exit codes:
    0  user created, or a user already existed (idempotent re-run)
    1  invalid input or creation failed
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Add parent directory to path so we can import from preloop.
sys.path.insert(0, str(Path(__file__).parent.parent))

from preloop.api.auth.jwt import get_password_hash
from preloop.models.crud import crud_account, crud_role, crud_user, crud_user_role
from preloop.models.db.session import get_db_session
from preloop.services.account_setup_service import complete_new_account_setup

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the first Preloop user.")
    parser.add_argument(
        "-u",
        "--username",
        default=os.getenv("PRELOOP_ADMIN_USERNAME", ""),
        help="username (env: PRELOOP_ADMIN_USERNAME)",
    )
    parser.add_argument(
        "-e",
        "--email",
        default=os.getenv("PRELOOP_ADMIN_EMAIL", ""),
        help="email address (env: PRELOOP_ADMIN_EMAIL)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.getenv("PRELOOP_ADMIN_PASSWORD", ""),
        help="password (env: PRELOOP_ADMIN_PASSWORD; prefer the env var)",
    )
    parser.add_argument(
        "--full-name",
        default=os.getenv("PRELOOP_ADMIN_FULL_NAME", ""),
        help="display name (optional)",
    )
    parser.add_argument(
        "--superuser",
        action="store_true",
        default=os.getenv("PRELOOP_ADMIN_SUPERUSER", "true").lower()
        in ("1", "true", "yes"),
        help="grant platform-wide admin privileges (default: true)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    missing = [
        name
        for name, value in (
            ("username", args.username),
            ("email", args.email),
            ("password", args.password),
        )
        if not str(value).strip()
    ]
    if missing:
        print(
            f"error: missing required value(s): {', '.join(missing)}", file=sys.stderr
        )
        return 1
    if len(args.password) < 8:
        print("error: password must be at least 8 characters", file=sys.stderr)
        return 1

    db = next(get_db_session())
    try:
        # "First user" means first: an instance that already has users is not
        # ours to bootstrap, and silently minting an owner account on it would
        # be a privilege-escalation footgun.
        if crud_user.get_multi(db, limit=1):
            print("A user already exists; skipping first-user creation.")
            return 0

        account = crud_account.create(
            db,
            obj_in={
                "organization_name": f"{args.username}'s Organization",
                "is_active": True,
            },
        )
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account.id,
                "username": args.username,
                "email": args.email,
                "hashed_password": get_password_hash(args.password),
                "full_name": args.full_name or None,
                "is_active": True,
                # No SMTP on a fresh install, so an unverified address would
                # lock the operator out of their own instance.
                "email_verified": True,
                "is_superuser": bool(args.superuser),
                "user_source": "local",
            },
        )
        account.primary_user_id = user.id
        db.add(account)

        owner_role = crud_role.get_by_name(db, name="owner")
        if owner_role:
            crud_user_role.create(
                db, obj_in={"user_id": user.id, "role_id": owner_role.id}
            )
        else:
            logger.warning("Owner role not found; user will have no permissions")

        db.commit()
        db.refresh(user)
        db.refresh(account)

        # Same post-signup setup the API performs (default approval workflow
        # and friends), minus the verification email.
        complete_new_account_setup(
            account_id=account.id,
            user_id=user.id,
            user_email=user.email,
            username=user.username,
            full_name=user.full_name,
            organization_name=account.organization_name,
            signup_source="install_script",
            send_verification=False,
        )

        print(f"Created first user '{user.username}' <{user.email}>.")
        return 0
    except Exception as exc:  # noqa: BLE001 - surface any failure to the installer
        db.rollback()
        print(f"error: failed to create first user: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
