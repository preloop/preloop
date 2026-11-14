"""Upgrade and downgrade MFA storage on a populated PostgreSQL schema."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.models import models


def test_downgrade_removes_only_mfa_storage(
    db_session: Session, test_user: models.User
) -> None:
    """protects=downgrade removes both tables and policy column, preserving users;
    fails_when=identity rows disappear; no_claim=large-table lock duration."""
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261208_user_mfa"
    )
    user_id = test_user.id
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
    inspector = inspect(db_session.connection())
    assert "user_mfa_factor" not in inspector.get_table_names()
    assert "user_mfa_recovery_code" not in inspector.get_table_names()
    assert "step_up_window_minutes" not in {
        c["name"] for c in inspector.get_columns("account")
    }
    assert (
        db_session.execute(
            text('SELECT id FROM "user" WHERE id=:id'), {"id": user_id}
        ).scalar_one()
        == user_id
    )


def test_upgrade_existing_accounts_defaults_off(
    db_session: Session, test_user: models.User
) -> None:
    """protects=existing accounts upgrade with policy off and correct proof columns;
    fails_when=upgrade fails or changes defaults; no_claim=large-table lock duration."""
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261208_user_mfa"
    )
    account_id = test_user.account_id
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        migration.upgrade()
    inspector = inspect(db_session.connection())
    assert {"secret_encrypted", "confirmed_at", "last_used_step"} <= {
        c["name"] for c in inspector.get_columns("user_mfa_factor")
    }
    assert {"code_hash", "used_at"} <= {
        c["name"] for c in inspector.get_columns("user_mfa_recovery_code")
    }
    assert (
        db_session.execute(
            text("SELECT step_up_window_minutes FROM account WHERE id=:id"),
            {"id": account_id},
        ).scalar_one()
        == 0
    )
