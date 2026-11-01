"""Selective session migration can roll back and upgrade a populated account."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from preloop.models import models


def test_enforcement_schema_round_trip(db_session, test_user):
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261115_sso_enforcement.py"
    )
    spec = importlib.util.spec_from_file_location("enforcement_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert "sso_break_glass_notice" not in inspect(connection).get_table_names()
        assert "non_sso_auth_generation" not in {
            c["name"] for c in inspect(connection).get_columns("user")
        }
        migration.upgrade()
    assert (
        db_session.scalar(
            select(models.User.non_sso_auth_generation).where(
                models.User.id == test_user.id
            )
        )
        == 0
    )
    for table in (
        "oauth_mcp_authorization_code",
        "oauth_mcp_access_token",
        "oauth_mcp_refresh_token",
    ):
        columns = {c["name"] for c in inspect(connection).get_columns(table)}
        assert {"auth_context", "auth_generation"} <= columns
    assert "sso_break_glass_notice" in inspect(connection).get_table_names()
