"""Refusal lookup migration preserves evidence and repairs an interrupted index."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, text

from tests.models import test_audit_lookup_index_migration as helpers


@pytest.fixture
def denial_index_connection(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Connection]:
    """Run concurrent DDL only against a disposable test schema."""
    monkeypatch.setattr(
        helpers,
        "MIGRATION_PATH",
        helpers.MIGRATION_PATH.with_name("20261220_denial_reason_idx.py"),
    )
    yield from helpers.isolated_audit_connection.__wrapped__(db_engine)


def test_upgrade_retry_and_downgrade_preserve_evidence(
    denial_index_connection: Connection,
) -> None:
    """Reapplying the migration leaves the data and valid index intact."""
    db = denial_index_connection
    helpers.run_migration(db, "upgrade")
    state = helpers.index_state(db)
    assert set(state) == {"ix_audit_log_account_reason_code"}
    assert "reason_code" in state["ix_audit_log_account_reason_code"][2]
    helpers.run_migration(db, "upgrade")
    assert helpers.index_state(db) == state
    db.execute(
        text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
            "'ix_audit_log_account_reason_code'::regclass"
        )
    )
    helpers.run_migration(db, "upgrade")
    assert helpers.index_state(db)["ix_audit_log_account_reason_code"][1]
    helpers.run_migration(db, "downgrade")
    assert helpers.index_state(db) == {}
    assert db.scalar(text("SELECT count(*) FROM audit_log")) == 1
