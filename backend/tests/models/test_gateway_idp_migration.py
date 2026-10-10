"""Migration ``20261010_gateway_idp`` (#1414): round trip down and up."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261010_gateway_idp.py"
)
TABLES = ("gateway_identity_provider", "gateway_identity_provider_audience")


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "gateway_idp_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_then_upgrade_restores_the_model_shape(db_session):
    migration = _load_migration()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        names = inspect(connection).get_table_names()
        assert not set(TABLES) & set(names)
        migration.upgrade()

    inspector = inspect(connection)
    provider_columns = {
        c["name"] for c in inspector.get_columns("gateway_identity_provider")
    }
    assert provider_columns == {
        c.name for c in models.GatewayIdentityProvider.__table__.columns
    }
    audience_columns = {
        c["name"] for c in inspector.get_columns("gateway_identity_provider_audience")
    }
    assert audience_columns == {
        c.name for c in models.GatewayIdentityProviderAudience.__table__.columns
    }
    unique = {
        u["name"]
        for u in inspector.get_unique_constraints("gateway_identity_provider_audience")
    }
    assert "uq_gateway_idp_audience_issuer_audience" in unique
    unique = {
        u["name"] for u in inspector.get_unique_constraints("gateway_identity_provider")
    }
    assert "uq_gateway_identity_provider_api_key" in unique
