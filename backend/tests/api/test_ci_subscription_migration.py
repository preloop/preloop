"""Immutable review attribution migration on disposable PostgreSQL schemas."""

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


@pytest.mark.parametrize("retained", [False, True])
def test_subscription_migration_no_backfill_and_guarded_downgrade(
    db_session: Session,
    retained: bool,
) -> None:
    path = (
        Path(__file__).parents[2]
        / "preloop/models/alembic/versions/20261004_ci_subscription_binding.py"
    )
    specification = importlib.util.spec_from_file_location(
        "ci_subscription_migration", path
    )
    assert specification is not None and specification.loader is not None
    migration = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(migration)
    connection = db_session.connection()
    with connection.begin_nested() as savepoint:
        schema = f"ci_subscription_migration_{uuid4().hex}"
        connection.execute(CreateSchema(schema))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        executions = sa.Table(
            "webhook_endpoint",
            sa.MetaData(),
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("account_id", UUID(as_uuid=True)),
            sa.Column("event_types", sa.dialects.postgresql.JSONB),
            sa.Column("source", sa.String),
            sa.Column("approval_workflow_id", UUID(as_uuid=True)),
            sa.Column("created_by_user_id", UUID(as_uuid=True)),
            sa.Column("ci_principal_id", UUID(as_uuid=True)),
            sa.Column("initiating_ci_key_id", UUID(as_uuid=True)),
        )
        executions.create(connection)
        old_id, flow_id = uuid4(), uuid4()
        connection.execute(executions.insert().values(id=old_id, account_id=flow_id))
        operations = Operations(MigrationContext.configure(connection))
        with Operations.context(operations.migration_context):
            migration.upgrade()
            assert (
                connection.execute(
                    sa.text("SELECT ci_subscription_binding FROM webhook_endpoint")
                ).scalar_one()
                is None
            )
            # A malformed key-only endpoint cannot shed its final machine
            # marker on key deletion and silently become an account-wide target.
            key_only = uuid4()
            connection.execute(
                sa.text(
                    "INSERT INTO webhook_endpoint (id, initiating_ci_key_id) VALUES (:id, :key)"
                ),
                {"id": key_only, "key": uuid4()},
            )
            with connection.begin_nested() as guarded:
                with pytest.raises(sa.exc.DBAPIError, match="attribution is immutable"):
                    connection.execute(
                        sa.text(
                            "UPDATE webhook_endpoint SET initiating_ci_key_id = NULL WHERE id = :id"
                        ),
                        {"id": key_only},
                    )
                guarded.rollback()
            if retained:
                table = sa.Table(
                    "webhook_endpoint", sa.MetaData(), autoload_with=connection
                )
                execution_id, principal_id, key_id = uuid4(), uuid4(), uuid4()
                binding = {"pr_number": 7, "head_sha": "a" * 40, "key_id": str(key_id)}
                connection.execute(
                    table.insert().values(
                        id=execution_id,
                        account_id=flow_id,
                        ci_principal_id=principal_id,
                        initiating_ci_key_id=key_id,
                        ci_subscription_binding=binding,
                    )
                )
                for values in [
                    {"ci_principal_id": uuid4()},
                    {"account_id": uuid4()},
                    {"event_types": ["approval.requested"]},
                    {"source": "approval_workflow"},
                    {"approval_workflow_id": uuid4()},
                    {"created_by_user_id": uuid4()},
                    {"initiating_ci_key_id": uuid4()},
                    {"ci_subscription_binding": None},
                ]:
                    with connection.begin_nested() as guarded:
                        with pytest.raises(
                            sa.exc.DBAPIError, match="attribution is immutable"
                        ):
                            connection.execute(
                                table.update()
                                .where(table.c.id == execution_id)
                                .values(**values)
                            )
                        guarded.rollback()
                connection.execute(
                    table.update()
                    .where(table.c.id == execution_id)
                    .values(initiating_ci_key_id=None)
                )
                assert (
                    connection.execute(
                        sa.select(table.c.ci_subscription_binding).where(
                            table.c.id == execution_id
                        )
                    ).scalar_one()
                    == binding
                )
                with connection.begin_nested() as guarded:
                    with pytest.raises(
                        sa.exc.DBAPIError,
                        match="Cannot erase CI subscription correlation",
                    ):
                        migration.downgrade()
                    guarded.rollback()
            else:
                migration.downgrade()
                assert "ci_subscription_binding" not in {
                    column["name"]
                    for column in sa.inspect(connection).get_columns(
                        "webhook_endpoint", schema=schema
                    )
                }
        savepoint.rollback()
