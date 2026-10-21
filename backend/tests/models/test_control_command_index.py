"""Durable control results must avoid scanning unrelated JSON activity."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from preloop.models.models.runtime_session_activity import RuntimeSessionActivity

INDEX = "ix_rsa_account_command_lookup"


def test_control_lookup_index_plans_and_repairs_interrupted_build(db_engine):
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261106_control_command_idx.py"
    )
    spec = importlib.util.spec_from_file_location("control_command_idx", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"control_index_{uuid4().hex}"
    account_id, command_id = str(uuid4()), str(uuid4())
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.execute(
                text(
                    "CREATE TABLE runtime_session_activity (id uuid PRIMARY KEY, account_id uuid, timestamp timestamptz, activity_type text, metadata jsonb)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO runtime_session_activity SELECT gen_random_uuid(), CAST(:account AS uuid), now(), 'tool_call', "
                    "jsonb_build_object('output', repeat('unrelated activity ', 200)) FROM generate_series(1, 20000)"
                ),
                {"account": account_id},
            )
            connection.execute(
                text(
                    "INSERT INTO runtime_session_activity VALUES (gen_random_uuid(), CAST(:account AS uuid), now(), 'agent_control_message', "
                    "jsonb_build_object('command_id', CAST(:command AS text), 'source', 'agent_control_result'))"
                ),
                {"account": account_id, "command": command_id},
            )

            original_timeouts = connection.execute(
                text(
                    "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
                )
            ).one()

            def run_migration(action):
                connection.commit()
                context = MigrationContext.configure(connection)
                with context.begin_transaction(), Operations.context(context):
                    action()
                assert (
                    connection.execute(
                        text(
                            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
                        )
                    ).one()
                    == original_timeouts
                )

            run_migration(migration.upgrade)
            connection.execute(text("ANALYZE runtime_session_activity"))
            for condition in (
                "activity_type = 'agent_control_message' AND (metadata ->> 'source') IS DISTINCT FROM 'agent_control_progress' ORDER BY timestamp DESC",
                "(metadata ->> 'source') = 'agent_control_result'",
            ):
                plan = connection.execute(
                    text(
                        "EXPLAIN (FORMAT JSON) SELECT * FROM runtime_session_activity "
                        "WHERE account_id = CAST(:account AS uuid) AND (metadata ->> 'command_id') = :command AND "
                        + condition
                        + " LIMIT 1"
                    ),
                    {"account": account_id, "command": command_id},
                ).scalar()
                assert INDEX in str(plan), plan
            before = connection.execute(
                text(f"SELECT '{INDEX}'::regclass::oid")
            ).scalar()
            run_migration(migration.upgrade)
            assert (
                connection.execute(text(f"SELECT '{INDEX}'::regclass::oid")).scalar()
                == before
            )
            connection.execute(
                text(
                    f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{INDEX}'::regclass"
                )
            )
            run_migration(migration.upgrade)
            assert (
                connection.execute(
                    text(
                        f"SELECT indisvalid FROM pg_index WHERE indexrelid = '{INDEX}'::regclass"
                    )
                ).scalar()
                is True
            )
            run_migration(migration.downgrade)
            assert (
                connection.execute(text(f"SELECT to_regclass('{INDEX}')")).scalar()
                is None
            )
        finally:
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def test_model_declares_the_same_partial_expression_index():
    index = next(i for i in RuntimeSessionActivity.__table__.indexes if i.name == INDEX)
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "account_id, (metadata ->> 'command_id'), timestamp DESC" in ddl
    assert "WHERE (metadata ->> 'command_id') IS NOT NULL" in ddl
