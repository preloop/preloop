"""Run the identity migration against isolated PostgreSQL schemas."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema


@pytest.mark.parametrize("retained_identity", [False, True])
def test_migration_preserves_legacy_and_refuses_attribution_erasure(
    db_session: Session, retained_identity: bool
) -> None:
    migration_path = (
        Path(__file__).parents[2]
        / "preloop/models/alembic/versions/20261004_ci_principal.py"
    )
    specification = importlib.util.spec_from_file_location(
        "ci_identity_migration", migration_path
    )
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    connection = db_session.connection()
    # No shared tables are altered. DDL and search_path roll back together.
    with connection.begin_nested() as savepoint:
        schema = f"ci_migration_{uuid4().hex}"
        connection.execute(CreateSchema(schema))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        metadata = sa.MetaData()
        ids = {}
        for name in (
            "account",
            "user",
            "project",
            "flow",
            "api_key",
            "flow_execution",
            "webhook_endpoint",
        ):
            columns = [sa.Column("id", UUID(as_uuid=True), primary_key=True)]
            if name == "api_key":
                columns += [
                    sa.Column("key", sa.String()),
                    sa.Column("scopes", sa.JSON()),
                ]
            table = sa.Table(name, metadata, *columns)
            table.create(connection)
            ids[name] = uuid4()
            values = {"id": ids[name]}
            if name == "api_key":
                values.update(key="synthetic-legacy-value", scopes=["mcp:read"])
            connection.execute(table.insert().values(**values))
        operations = Operations(MigrationContext.configure(connection))
        with Operations.context(operations.migration_context):
            migration.upgrade()
            legacy = connection.execute(
                sa.text(
                    "SELECT key, scopes, credential_type, credential_version, ci_principal_id FROM api_key"
                )
            ).one()
            assert legacy == (
                "synthetic-legacy-value",
                ["mcp:read"],
                "legacy",
                None,
                None,
            )
            for table in ("flow_execution", "webhook_endpoint"):
                assert connection.execute(
                    sa.text(
                        f"SELECT ci_principal_id, initiating_ci_key_id FROM {table}"
                    )
                ).one() == (None, None)
            if retained_identity:
                principals = sa.Table(
                    "ci_principal", sa.MetaData(), autoload_with=connection
                )
                connection.execute(
                    principals.insert().values(
                        id=uuid4(),
                        name="Retained synthetic identity",
                        account_id=ids["account"],
                        administered_by_user_id=ids["user"],
                        is_active=False,
                        credential_version=1,
                        project_id=ids["project"],
                        flow_id=ids["flow"],
                        tracker_id=uuid4(),
                        repository_identifier="synthetic",
                        repository_binding={},
                        grant={},
                    )
                )
                with pytest.raises(RuntimeError, match="retained identities"):
                    migration.downgrade()
                assert (
                    connection.execute(
                        sa.text("SELECT count(*) FROM ci_principal")
                    ).scalar_one()
                    == 1
                )
            else:
                migration.downgrade()
                assert connection.execute(
                    sa.text("SELECT key, scopes FROM api_key")
                ).one() == ("synthetic-legacy-value", ["mcp:read"])
        savepoint.rollback()
