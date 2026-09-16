"""The `parent_session_id` column: shape, reversal, and what null means.

Child of the subagent session identity work (#638). The column is additive and
nullable by design: every session that already exists, and every session whose
harness never says what spawned it, reads back null. These tests pin that,
plus the foreign key that keeps a recorded parent pointing at a real session.
"""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.audit_chain import FOREIGN_KEY_VIOLATION, postgres_sqlstate

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260915_runtime_session_parent.py"
)
INDEX_NAME = "ix_runtime_session_parent_session_id"
FK_NAME = "fk_runtime_session_parent_session_id"


def _migration():
    """Load the revision off disk, as the sibling migration tests do."""
    spec = importlib.util.spec_from_file_location(
        "runtime_session_parent_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(db_session: Session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().downgrade()


def _upgrade(db_session: Session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().upgrade()


def _table_columns(db_session: Session) -> set[str]:
    return {
        column["name"]
        for column in inspect(db_session.connection()).get_columns("runtime_session")
    }


def _session_row(db_session: Session, test_user, **kwargs) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="opencode",
        session_source_id=f"src-{uuid.uuid4().hex[:8]}",
        started_at=datetime.now(UTC),
        **kwargs,
    )
    db_session.add(session)
    db_session.flush()
    return session


def test_existing_sessions_read_back_null_after_the_upgrade(db_session, test_user):
    """A session written before the upgrade keeps working, with no parent.

    Nothing backfills lineage: the harness signal that would identify a parent
    is only on the wire, so history is unknowable and stays null.
    """
    _downgrade(db_session)
    assert "parent_session_id" not in _table_columns(db_session)

    legacy_id = uuid.uuid4()
    now = datetime.now(UTC)
    db_session.execute(
        text(
            "INSERT INTO runtime_session "
            "(id, account_id, session_source_type, session_source_id, "
            " started_at, created_at, updated_at) "
            "VALUES (:id, :account_id, :source_type, :source_id, "
            " :started_at, :created_at, :updated_at)"
        ),
        {
            "id": legacy_id,
            "account_id": test_user.account_id,
            "source_type": "opencode",
            "source_id": "legacy-session",
            "started_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )

    _upgrade(db_session)

    assert "parent_session_id" in _table_columns(db_session)
    # The model and the revision have to agree, or the next autogenerate run
    # would try to drop what this one added.
    assert "parent_session_id" in {
        column.name for column in models.RuntimeSession.__table__.columns
    }
    legacy = db_session.execute(
        select(models.RuntimeSession).filter(models.RuntimeSession.id == legacy_id)
    ).scalar_one()
    assert legacy.parent_session_id is None


def test_migration_reverses_cleanly(db_session):
    """Downgrade removes exactly what upgrade added, index and FK included."""
    original_columns = _table_columns(db_session)

    _downgrade(db_session)

    assert _table_columns(db_session) == original_columns - {"parent_session_id"}
    downgraded = inspect(db_session.connection())
    assert INDEX_NAME not in {
        index["name"] for index in downgraded.get_indexes("runtime_session")
    }
    assert FK_NAME not in {
        fk["name"] for fk in downgraded.get_foreign_keys("runtime_session")
    }

    _upgrade(db_session)

    assert _table_columns(db_session) == original_columns
    inspector = inspect(db_session.connection())
    assert INDEX_NAME in {
        index["name"] for index in inspector.get_indexes("runtime_session")
    }
    parent_fk = [
        fk
        for fk in inspector.get_foreign_keys("runtime_session")
        if fk["constrained_columns"] == ["parent_session_id"]
    ]
    assert len(parent_fk) == 1
    assert parent_fk[0]["referred_table"] == "runtime_session"
    assert parent_fk[0]["referred_columns"] == ["id"]
    # A deleted parent must not take its children's rows or spend with it.
    assert parent_fk[0]["options"].get("ondelete") == "SET NULL"
    column = {
        column["name"]: column for column in inspector.get_columns("runtime_session")
    }["parent_session_id"]
    assert column["nullable"] is True


def test_parent_id_must_point_at_a_real_session(db_session, test_user):
    """A dangling parent would make the lineage unreadable, so the FK refuses."""
    with pytest.raises(IntegrityError) as error:
        with db_session.begin_nested():
            _session_row(db_session, test_user, parent_session_id=uuid.uuid4())
    assert postgres_sqlstate(error.value) == FOREIGN_KEY_VIOLATION


def test_deleting_a_parent_leaves_the_child_with_no_parent(db_session, test_user):
    """Losing the parent row costs the link, never the child's own history."""
    parent = _session_row(db_session, test_user)
    child = _session_row(db_session, test_user, parent_session_id=parent.id)

    db_session.delete(parent)
    db_session.flush()
    db_session.refresh(child)

    assert child.parent_session_id is None
