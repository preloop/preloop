"""Initialize test data for Preloop."""

import asyncio
import logging
import uuid

from passlib.context import CryptContext
from sqlalchemy.exc import SQLAlchemyError

from preloop.models.crud import crud_role, crud_user, crud_user_role
from preloop.models.crud.account import CRUDAccount
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import CRUDProject
from preloop.models.crud.tracker import CRUDTracker
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker, TrackerType

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def create_test_data():
    """Create test data for the Preloop server."""
    session_generator = get_db_session()
    db = next(session_generator)
    crud_account = CRUDAccount(Account)
    crud_tracker = CRUDTracker(Tracker)
    crud_organization = CRUDOrganization(Organization)
    crud_project = CRUDProject(Project)

    try:
        # Create a test admin (modern account/user split: email, username and
        # password live on User; Account is the tenant shell — mirrors
        # scripts/create_first_user.py, the maintained bootstrap path).
        user = crud_user.get_by_email(db, email="admin@preloop.ai")
        if user:
            account = crud_account.get(db, id=user.account_id)
            logger.info(f"Admin user already exists (account ID: {account.id})")
        else:
            logger.info("Creating admin account and user...")
            account = crud_account.create(
                db,
                obj_in={
                    "organization_name": "Admin's Organization",
                    "is_active": True,
                    "meta_data": {"admin_account": True},
                },
            )
            user = crud_user.create(
                db,
                obj_in={
                    "account_id": account.id,
                    "username": "admin",
                    "email": "admin@preloop.ai",
                    "hashed_password": pwd_context.hash("admin"),
                    "full_name": "Admin User",
                    "is_active": True,
                    "email_verified": True,
                    "is_superuser": True,
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
            db.commit()
            db.refresh(account)
            logger.info(f"Created admin user (account ID: {account.id})")

        # Create a tracker if it doesn't exist
        tracker = crud_tracker.get_for_account(db, account_id=account.id)
        if tracker and len(tracker) > 0:
            logger.info(
                f"Tracker already exists: {tracker[0].name} (ID: {tracker[0].id})"
            )
            tracker = tracker[0]
        else:
            logger.info("Creating GitHub tracker...")
            tracker = crud_tracker.create(
                db,
                obj_in={
                    "id": str(uuid.uuid4()),
                    "name": "GitHub Issues",
                    "tracker_type": TrackerType.GITHUB.value,
                    "account_id": account.id,
                    "is_active": True,
                    "url": "https://api.github.com",
                    "api_key": "github_pat_mock_token",
                    "connection_details": {"repository": "spacecode/astrobot"},
                    "meta_data": {"integration_type": "personal_access_token"},
                },
            )
            logger.info(f"Created tracker: {tracker.name} (ID: {tracker.id})")

        # Create an organization if it doesn't exist
        org = crud_organization.get_by_identifier(db, identifier="spacecode")
        if org:
            logger.info(f"Organization already exists: {org.name} (ID: {org.id})")
        else:
            # Create organization
            logger.info("Creating test organization...")
            org = crud_organization.create(
                db,
                obj_in={
                    "id": str(uuid.uuid4()),
                    "name": "Spacecode AI",
                    "identifier": "spacecode",
                    "description": "Spacecode AI organization for testing",
                    "tracker_id": tracker.id,
                    "is_active": True,
                    "settings": {"default_tracker": str(tracker.id)},
                    "meta_data": {"industry": "Technology", "size": "Startup"},
                },
            )
            logger.info(f"Created organization: {org.name} (ID: {org.id})")

        # Create a project if it doesn't exist
        project = crud_project.get_by_identifier(
            db, identifier="astrobot", account_id=str(account.id)
        )
        if project:
            logger.info(f"Project already exists: {project.name} (ID: {project.id})")
        else:
            # Create project
            logger.info("Creating test project...")
            project = crud_project.create(
                db,
                obj_in={
                    "id": str(uuid.uuid4()),
                    "name": "Astrobot",
                    "identifier": "astrobot",
                    "description": "Astrobot project for testing",
                    "organization_id": org.id,
                    "is_active": True,
                    "settings": {"visibility": "public"},
                    "tracker_settings": {
                        "github": {
                            "repository": "spacecode/astrobot",
                            "credentials": "github_pat_mock_token",
                        }
                    },
                    "meta_data": {"team": "Engineering"},
                },
            )
            logger.info(f"Created project: {project.name} (ID: {project.id})")

        logger.info("Test data setup complete")

    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Error creating test data: {e}")
        raise
    finally:
        db.close()
        try:
            # Clean up the generator
            next(session_generator, None)
        except StopIteration:
            pass


def main():
    """Main function to create test data."""
    try:
        # First ensure the database tables exist using Alembic
        import os
        import subprocess
        from pathlib import Path

        # Get the models directory path (where alembic.ini lives)
        repo_root = Path(__file__).resolve().parents[1]
        models_dir = os.getenv(
            "PRELOOP_MODELS_PATH",
            str(repo_root / "backend" / "preloop" / "models"),
        )

        logger.info(
            "Running Alembic migrations to ensure database schema is up to date..."
        )

        # Run alembic upgrade head
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=models_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.error(f"Alembic migration failed: {result.stderr}")
            raise RuntimeError(f"Failed to run database migrations: {result.stderr}")

        logger.info("Database schema initialized successfully via Alembic")

        # Now create the test data
        asyncio.run(create_test_data())
        logger.info("Test data creation completed successfully")
    except Exception as e:
        logger.error(f"Failed to create test data: {e}")
        raise


if __name__ == "__main__":
    main()
