"""``20261207_user_passkey_only`` on a populated user table.

The column is non-null; without a server default the upgrade would abort on
any existing user, and the downgrade must not take user rows with it.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261207_user_passkey_only.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("passkey_only", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _columns(db_session) -> set[str]:
    return {c["name"] for c in inspect(db_session.connection()).get_columns("user")}


def test_downgrade_then_upgrade_keeps_users_and_defaults_false(db_session, create_user):
    """protects=the migration runs on a populated table in both directions;
    fails_when=downgrade drops user rows or leaves the column, or upgrade fails
    on existing rows or fills anything but false;
    no_claim=lock duration on a large production table."""
    user = create_user(username="pk_migration", email="pk_migration@example.com")
    db_session.execute(
        text('UPDATE "user" SET passkey_only = true WHERE id = :id'), {"id": user.id}
    )
    migration = _load_migration()
    context = MigrationContext.configure(db_session.connection())

    with Operations.context(context):
        migration.downgrade()
    assert "passkey_only" not in _columns(db_session)
    assert db_session.execute(
        text('SELECT id FROM "user" WHERE id = :id'), {"id": user.id}
    ).first()

    with Operations.context(context):
        migration.upgrade()
    assert "passkey_only" in _columns(db_session)
    value = db_session.execute(
        text('SELECT passkey_only FROM "user" WHERE id = :id'), {"id": user.id}
    ).scalar_one()
    assert value is False
