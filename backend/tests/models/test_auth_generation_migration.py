"""auth_generation must apply to users that already exist.

The column is non-null with server_default 0. A deploy that added it
without a default would abort on any populated user table.
"""

import importlib.util
from pathlib import Path

from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models.crud import crud_user

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260921_auth_generation.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "auth_generation_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_auth_generation_migration_fills_existing_users(db_session, create_user):
    """Drop the column, keep the user row, re-apply upgrade, assert gen 0."""
    user = create_user(username="legacy_gen", email="legacy_gen@example.com")
    db_session.flush()

    context = MigrationContext.configure(db_session.connection())
    with Operations.context(context):
        op.drop_column("user", "auth_generation")

    columns = {
        col["name"] for col in inspect(db_session.connection()).get_columns("user")
    }
    assert "auth_generation" not in columns

    remaining = db_session.execute(
        text('SELECT id FROM "user" WHERE id = :id'), {"id": user.id}
    ).first()
    assert remaining is not None

    migration = _load_migration()
    with Operations.context(context):
        migration.upgrade()

    db_session.expire_all()
    restored = crud_user.get(db_session, id=user.id)
    assert restored is not None
    assert restored.auth_generation == 0
