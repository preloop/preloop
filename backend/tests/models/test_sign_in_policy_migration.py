"""Account policy upgrade/downgrade against existing PostgreSQL data."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.models import models


def test_policy_column_round_trip(db_session: Session, test_user: models.User) -> None:
    """protects=upgrade defaults off and downgrade preserves members and factors;
    fails_when=policy disappears incorrectly or existing account becomes enforced;
    no_claim=large-table migration lock duration."""
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261116_sign_in_policy"
    )
    account_id = test_user.account_id
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        assert "strong_sign_in_policy" not in {
            column["name"]
            for column in inspect(db_session.connection()).get_columns("account")
        }
        assert "user_mfa_factor" in inspect(db_session.connection()).get_table_names()
        migration.upgrade()
    assert (
        db_session.execute(
            text("SELECT strong_sign_in_policy FROM account WHERE id=:id"),
            {"id": account_id},
        ).scalar_one()
        == "off"
    )
    assert (
        db_session.execute(
            text('SELECT id FROM "user" WHERE id=:id'), {"id": test_user.id}
        ).scalar_one()
        == test_user.id
    )
