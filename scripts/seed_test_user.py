#!/usr/bin/env python
"""
Script to seed the database with a test user and API key.
"""

import os
import click
from dotenv import load_dotenv

from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.models.db.session import get_db_session
from preloop.models.db.setup import setup_database
from preloop.api.auth.jwt import get_password_hash
from preloop.schemas.auth import ApiKeyCreate


def assign_owner_role_if_available(db_session, user):
    """Assign owner role to user if RBAC system is available.

    This is a no-op in open-source builds without RBAC.
    In Enterprise builds, assigns the 'owner' role to give full access.
    """
    try:
        from preloop.models.crud.permission import crud_user_role
        from preloop.models.models.permission import Role

        # Check if user already has owner role
        user_roles = crud_user_role.get_user_roles(db_session, user_id=user.id)
        if any(r.name == "owner" for r in user_roles):
            click.echo("User already has owner role.")
            return True

        # Find the owner role
        owner_role = (
            db_session.query(Role)
            .filter(Role.name == "owner", Role.is_system_role.is_(True))
            .first()
        )

        if not owner_role:
            click.echo("Owner role not found - trying to initialize system roles...")
            try:
                # Import dynamically to support OS fallback cleanly
                from preloop.scripts.init_system_roles import initialize_system_roles

                initialize_system_roles(db_session)
                owner_role = (
                    db_session.query(Role)
                    .filter(Role.name == "owner", Role.is_system_role.is_(True))
                    .first()
                )
            except Exception as e:
                click.echo(f"Warning: Could not initialize system roles: {e}")

        if owner_role:
            crud_user_role.assign_role(
                db_session,
                user_id=user.id,
                role_id=owner_role.id,
                granted_by=None,  # System-assigned
            )
            db_session.commit()
            click.echo("Assigned 'owner' role to user.")
            return True
        else:
            click.echo("Owner role not found - RBAC could not be initialized.")
            return False

    except ImportError:
        # RBAC not available (open-source build)
        click.echo("RBAC not available - skipping role assignment.")
        return False
    except Exception as e:
        click.echo(f"Warning: Could not assign owner role: {e}")
        return False


@click.command()
@click.option(
    "--email", envvar="TEST_USER_EMAIL", required=True, help="Email for the test user."
)
@click.option(
    "--password",
    envvar="TEST_USER_PASSWORD",
    required=True,
    help="Password for the test user.",
)
@click.option(
    "--api-key",
    envvar="TEST_USER_API_KEY",
    required=True,
    help="API key for the test user.",
)
def seed_test_user(email: str, password: str, api_key: str):
    """
    Creates a test user and an API key if they don't already exist.
    """
    load_dotenv()
    setup_database(os.getenv("DATABASE_URL"))
    db_session = next(get_db_session())

    click.echo(f"Checking for test user with email: {email}")
    user = crud_user.get_by_email(db_session, email=email)

    if user:
        click.echo("Test user already exists.")
    else:
        click.echo("Creating new test user...")
        # First create an account (organization)
        account_in = {
            "organization_name": "Test Organization",
            "is_active": True,
            "email_verified": True,
        }
        account = crud_account.create(db_session, obj_in=account_in)
        db_session.flush()  # Ensure account.id is available

        # Then create a user with authentication credentials
        user_in = {
            "account_id": str(account.id),
            "email": email,
            "username": os.getenv("TEST_USER_USERNAME", "test_user"),
            "full_name": "Test User",
            "hashed_password": get_password_hash(password),
            "email_verified": True,
            "is_active": True,
            "user_source": "local",
        }
        user = crud_user.create(db_session, obj_in=user_in)
        db_session.flush()  # Ensure user.id is available

        # Update account to set primary_user_id
        account.primary_user_id = user.id
        db_session.commit()

        click.echo("Test user and account created successfully.")

    # Assign owner role if RBAC is available (Enterprise Edition)
    click.echo("Checking for RBAC role assignment...")
    assign_owner_role_if_available(db_session, user)

    click.echo("Checking for test user API key...")
    if crud_api_key.get_by_key(
        db_session, key=api_key, account_id=str(user.account_id)
    ):
        click.echo("API key already exists for the test user.")
    else:
        click.echo("Creating API key for the test user...")
        api_key_in = ApiKeyCreate(name="Test API Key")
        crud_api_key.create_with_owner(
            db_session,
            obj_in=api_key_in.model_dump(),
            owner_username=user.username,
            key_value=api_key,
        )
        click.echo("API key created successfully.")

    click.echo("\nDatabase seeding complete.")


if __name__ == "__main__":
    seed_test_user()
