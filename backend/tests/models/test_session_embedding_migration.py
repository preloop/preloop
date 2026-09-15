"""The embedding migration builds the vector index, not just the column.

A vector column with no index is a table scan wearing a cosine distance, so
the index is the part of this migration worth a test: it has to exist, be
HNSW, use cosine ops and cover only the rows that actually carry a vector.
The upgrade is exercised after a downgrade in the same transaction, which is
also how a rollback would be run in anger.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models import models
from preloop.models.models.session_search_document import EMBEDDING_DIMENSIONS

VERSIONS = Path(__file__).resolve().parents[2] / "preloop/models/alembic/versions"
MIGRATION_PATH = VERSIONS / "20260915_session_embedding.py"
#: The opt in table's shape is these two migrations together, so the model
#: comparison below has to run both.
SCOPE_MIGRATION_PATH = VERSIONS / "20260916_session_embedding_scope.py"
INDEX_NAME = "ix_session_search_document_embedding"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _rerun_migration(db_session) -> None:
    migration = _load(MIGRATION_PATH, "session_embedding_migration")
    scope_migration = _load(SCOPE_MIGRATION_PATH, "session_embedding_scope_migration")
    with Operations.context(MigrationContext.configure(db_session.connection())):
        scope_migration.downgrade()
        migration.downgrade()
        migration.upgrade()
        scope_migration.upgrade()


def _index_definition(db_session, name: str) -> str:
    return db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
        {"name": name},
    ).scalar_one()


def test_upgrade_builds_the_vector_index_over_rows_that_have_a_vector(db_session):
    """The index is HNSW, cosine, and restricted to non NULL embeddings."""
    _rerun_migration(db_session)

    definition = _index_definition(db_session, INDEX_NAME)
    assert "USING hnsw" in definition
    assert "vector_cosine_ops" in definition
    # Without the predicate the build would walk every chunk to index the few
    # that are embedded, on a corpus that is mostly NULL by design.
    assert "WHERE (embedding IS NOT NULL)" in definition


def test_upgrade_adds_the_vector_columns_at_the_declared_width(db_session):
    """The stored width and the model's constant may never drift apart."""
    _rerun_migration(db_session)

    columns = {
        column["name"]: column
        for column in inspect(db_session.connection()).get_columns(
            "session_search_document"
        )
    }
    for name in ("embedding", "embedding_model", "embedded_at", "embedding_attempts"):
        assert name in columns

    width = db_session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'session_search_document'::regclass "
            "AND attname = 'embedding'"
        )
    ).scalar_one()
    assert width == EMBEDDING_DIMENSIONS


def test_upgrade_creates_the_opt_in_table_matching_the_model(db_session):
    """The opt in table is the account's decision; its shape is the model's."""
    _rerun_migration(db_session)

    actual = {
        column["name"]
        for column in inspect(db_session.connection()).get_columns(
            models.SessionEmbeddingSetting.__tablename__
        )
    }
    assert actual == {
        column.name for column in models.SessionEmbeddingSetting.__table__.columns
    }
    account_index = _index_definition(
        db_session, "ix_session_embedding_setting_account_id"
    )
    # One row per account: a second row would be a second provider decision
    # for the same customer text.
    assert "CREATE UNIQUE INDEX" in account_index


def test_downgrade_removes_the_vector_index_and_columns(db_session):
    """A rollback leaves the keyword corpus intact and the vectors gone."""
    migration = _load(MIGRATION_PATH, "session_embedding_migration")
    scope_migration = _load(SCOPE_MIGRATION_PATH, "session_embedding_scope_migration")
    with Operations.context(MigrationContext.configure(db_session.connection())):
        scope_migration.downgrade()
        migration.downgrade()

    columns = {
        column["name"]
        for column in inspect(db_session.connection()).get_columns(
            "session_search_document"
        )
    }
    assert "embedding" not in columns
    assert "content" in columns
    assert (
        db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": INDEX_NAME},
        ).first()
        is None
    )
