"""The scope migration backfills, rather than leaving a column nobody set.

The value of the column is its default. A row that upgrades into a NULL or
into ``full`` would mean an account that never asked for transcript
embedding starts paying for it, so the backfill is the part worth asserting:
every row that existed before the upgrade reads ``summaries_only`` after it.
"""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models.crud import crud_account
from preloop.models.models.session_embedding_setting import (
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260916_session_embedding_scope.py"
)
TABLE = "session_embedding_setting"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "session_embedding_scope_migration", MIGRATION_PATH
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _columns(db_session) -> set:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(TABLE)
    }


def test_downgrade_then_upgrade_backfills_existing_rows(db_session):
    """A row written before the column exists reads the default after it."""
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Scope Migration Org", "is_active": True},
    )
    migration = _migration()
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        assert "scope" not in _columns(db_session)
        # Written while the column does not exist, which is exactly the row
        # an upgrade meets in a deployment that already opted in.
        db_session.execute(
            text(
                f"INSERT INTO {TABLE} (id, account_id, enabled, provider, "
                "dimensions) VALUES (:id, :account_id, true, "
                "'openai_compatible', 1536)"
            ),
            {"id": str(uuid.uuid4()), "account_id": str(account.id)},
        )
        migration.upgrade()

    assert "scope" in _columns(db_session)
    scopes = (
        db_session.execute(
            text(f"SELECT scope FROM {TABLE} WHERE account_id = :account_id"),
            {"account_id": str(account.id)},
        )
        .scalars()
        .all()
    )
    assert scopes == [EMBEDDING_SCOPE_SUMMARIES_ONLY]


def test_the_column_is_not_null_with_the_default_on_the_server(db_session):
    """An insert that says nothing about scope still lands on the default."""
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Scope Default Org", "is_active": True},
    )
    db_session.execute(
        text(
            f"INSERT INTO {TABLE} (id, account_id, enabled, provider, dimensions) "
            "VALUES (:id, :account_id, false, 'openai_compatible', 1536)"
        ),
        {"id": str(uuid.uuid4()), "account_id": str(account.id)},
    )

    row = db_session.execute(
        text(f"SELECT scope FROM {TABLE} WHERE account_id = :account_id"),
        {"account_id": str(account.id)},
    ).scalar_one()
    assert row == EMBEDDING_SCOPE_SUMMARIES_ONLY

    column = next(
        item
        for item in inspect(db_session.connection()).get_columns(TABLE)
        if item["name"] == "scope"
    )
    assert column["nullable"] is False
