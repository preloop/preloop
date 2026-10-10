"""Migration ``20261010_otlp_telemetry_ingest`` (#1412), down and up."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261010_otlp_telemetry_ingest.py"
)
INDEXES = {
    "ix_api_usage_account_upstream_request_id",
    "ix_api_usage_gateway_client_request_id",
    "ix_api_usage_otlp_client_request_id",
    "ix_api_usage_otlp_request_id",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "otlp_ingest_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _usage_indexes(connection) -> set[str]:
    return {index["name"] for index in inspect(connection).get_indexes("api_usage")}


def _insert_usage(connection, account_id, cost_source: str) -> None:
    connection.execute(
        text(
            "INSERT INTO api_usage (id, account_id, endpoint, method, status_code, "
            "duration, action_type, cost_source) VALUES (:id, :account, '/x', "
            "'POST', 200, 0, 'imported_usage', :cost_source)"
        ),
        {"id": uuid.uuid4(), "account": account_id, "cost_source": cost_source},
    )


def test_downgrade_then_upgrade(db_session, test_user):
    migration = _load_migration()
    connection = db_session.connection()
    _insert_usage(connection, test_user.account_id, "telemetry_estimate")
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        tables = inspect(connection).get_table_names()
        assert "telemetry_ingest_dedup" not in tables
        assert "telemetry_metric_series" not in tables
        assert not INDEXES & _usage_indexes(connection)
        # The telemetry row survived as 'imported'.
        assert (
            connection.execute(
                text("SELECT count(*) FROM api_usage WHERE cost_source = 'imported'")
            ).scalar()
            >= 1
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                _insert_usage(connection, test_user.account_id, "telemetry_estimate")
        migration.upgrade()

    assert INDEXES <= _usage_indexes(connection)
    for model in (models.TelemetryIngestDedup, models.TelemetryMetricSeries):
        columns = {
            c["name"] for c in inspect(connection).get_columns(model.__tablename__)
        }
        assert columns == {column.name for column in model.__table__.columns}
    _insert_usage(connection, test_user.account_id, "telemetry_estimate")


def test_dedup_key_is_unique_per_account(db_session, test_user):
    connection = db_session.connection()
    statement = text(
        "INSERT INTO telemetry_ingest_dedup (id, account_id, dedup_key, kind, seen_at) "
        "VALUES (:id, :account, :key, 'log', now())"
    )
    connection.execute(
        statement,
        {"id": uuid.uuid4(), "account": test_user.account_id, "key": "k" * 64},
    )
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(
                statement,
                {"id": uuid.uuid4(), "account": test_user.account_id, "key": "k" * 64},
            )
