"""Migration ``20261009_gateway_subject`` (#1409): round trip down and up."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261009_gateway_subject.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "gateway_subject_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_then_upgrade_restores_the_model_shape(db_session):
    migration = _load_migration()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert "gateway_subject" not in inspect(connection).get_table_names()
        migration.upgrade()

    columns = {c["name"] for c in inspect(connection).get_columns("gateway_subject")}
    assert columns == {c.name for c in models.GatewaySubject.__table__.columns}
    unique = {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints("gateway_subject")
    }
    assert "uq_gateway_subject_account_key_subject" in unique
