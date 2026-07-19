#!/usr/bin/env python3
"""Script to manage superuser privileges for users.

Usage:
    # Promote a user to superuser
    python scripts/manage_superusers.py promote <username_or_email>

    # Demote a user from superuser
    python scripts/manage_superusers.py demote <username_or_email>

    # List all superusers
    python scripts/manage_superusers.py list
"""

import sys
from pathlib import Path

# Import preloop from THIS repo's backend/, ahead of any installed package
# (an editable install may point at a different checkout).
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from sqlalchemy.orm import Session
from preloop.models.db.session import get_db_session
from preloop.models.models import User


def promote_superuser(username_or_email: str, db: Session):
    """Promote a user to superuser."""
    # Try to find user by username or email
    user = (
        db.query(User)
        .filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        )
        .first()
    )

    if not user:
        print(f"❌ User not found: {username_or_email}")
        sys.exit(1)

    if user.is_superuser:
        print(f"ℹ️  User {user.username} ({user.email}) is already a superuser")
        return

    user.is_superuser = True
    db.commit()
    print(f"✅ User {user.username} ({user.email}) promoted to superuser")


def demote_superuser(username_or_email: str, db: Session):
    """Demote a user from superuser."""
    # Try to find user by username or email
    user = (
        db.query(User)
        .filter(
            (User.username == username_or_email) | (User.email == username_or_email)
        )
        .first()
    )

    if not user:
        print(f"❌ User not found: {username_or_email}")
        sys.exit(1)

    if not user.is_superuser:
        print(f"ℹ️  User {user.username} ({user.email}) is not a superuser")
        return

    user.is_superuser = False
    db.commit()
    print(f"✅ User {user.username} ({user.email}) demoted from superuser")


def list_superusers(db: Session):
    """List all superusers."""
    superusers = db.query(User).filter(User.is_superuser).all()

    if not superusers:
        print("ℹ️  No superusers found")

        # Show all users as suggestions
        all_users = db.query(User).limit(10).all()
        if all_users:
            print("\n💡 Available users to promote:")
            print("=" * 80)
            for user in all_users:
                print(f"  • {user.username:20} {user.email:30}")
            print(
                "\n👉 Promote a user: python scripts/manage_superusers.py promote <username_or_email>"
            )
        print()
        return

    print(f"\n📋 Superusers ({len(superusers)}):")
    print("=" * 80)
    for user in superusers:
        print(f"  • {user.username:20} {user.email:30} (ID: {user.id})")
    print()


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    # Get database session
    db = next(get_db_session())

    try:
        if command == "promote":
            if len(sys.argv) < 3:
                print(
                    "❌ Usage: python scripts/manage_superusers.py promote <username_or_email>"
                )
                sys.exit(1)
            promote_superuser(sys.argv[2], db)

        elif command == "demote":
            if len(sys.argv) < 3:
                print(
                    "❌ Usage: python scripts/manage_superusers.py demote <username_or_email>"
                )
                sys.exit(1)
            demote_superuser(sys.argv[2], db)

        elif command == "list":
            list_superusers(db)

        else:
            print(f"❌ Unknown command: {command}")
            print(__doc__)
            sys.exit(1)

    finally:
        db.close()


if __name__ == "__main__":
    main()
