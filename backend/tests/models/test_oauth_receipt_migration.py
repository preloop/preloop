"""Receipt migration is reversible and leaves legacy expiry data unchanged."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


def test_oauth_receipt_migration_roundtrip(db_session: Session) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261009_oauth_receipt_anchor.py"
    )
    spec = importlib.util.spec_from_file_location("receipt_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = db_session.connection()
    migration.op = Operations(MigrationContext.configure(connection))
    before = connection.execute(
        text(
            "SELECT id, expires_at, refresh_token_expires_at FROM oauth_token ORDER BY id"
        )
    ).all()
    migration.downgrade()
    assert "issued_at" not in {
        column["name"] for column in inspect(connection).get_columns("oauth_token")
    }
    migration.upgrade()
    assert "issued_at" in {
        column["name"] for column in inspect(connection).get_columns("oauth_token")
    }
    assert (
        before
        == connection.execute(
            text(
                "SELECT id, expires_at, refresh_token_expires_at FROM oauth_token ORDER BY id"
            )
        ).all()
    )
    assert (
        connection.execute(
            text("SELECT count(*) FROM oauth_token WHERE issued_at IS NOT NULL")
        ).scalar()
        == 0
    )
