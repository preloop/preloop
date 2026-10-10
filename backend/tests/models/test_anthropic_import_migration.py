"""Migration ``20261010_anthropic_import`` (#1413): round-trip and shape."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261010_anthropic_import.py"
)
TABLES = ("anthropic_import_connection", "anthropic_user_mapping")


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "anthropic_import_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection, table):
    return {column["name"] for column in inspect(connection).get_columns(table)}


def test_downgrade_then_upgrade_restores_the_model_shape(db_session):
    migration = _load_migration()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        names = inspect(connection).get_table_names()
        for table in TABLES:
            assert table not in names
        migration.upgrade()

    for model in (models.AnthropicImportConnection, models.AnthropicUserMapping):
        assert _columns(connection, model.__tablename__) == {
            column.name for column in model.__table__.columns
        }
    unique = {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints(
            "anthropic_user_mapping"
        )
    }
    assert "uq_anthropic_user_mapping_actor" in unique


def test_revision_sits_on_the_previous_head():
    migration = _load_migration()
    assert migration.revision == "20261010_anthropic_import"
    assert migration.down_revision == "20261010_gateway_idp"
