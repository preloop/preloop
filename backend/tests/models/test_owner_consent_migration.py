"""Owner consent schema upgrades preserve resources and enforce safe modes."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models


def test_owner_consent_migration_round_trip(db_session: Session) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261022_owner_consent.py"
    )
    spec = importlib.util.spec_from_file_location("owner_consent_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert "owner_consent" not in inspect(connection).get_table_names()
        migration.upgrade()
    columns = {c["name"] for c in inspect(connection).get_columns("owner_consent")}
    assert columns == set(models.OwnerConsent.__table__.columns.keys())
    assert "flow_execution_consent" in {
        c["name"] for c in inspect(connection).get_columns("flow_runner")
    }
    assert "others_may_start_sessions" in {
        c["name"] for c in inspect(connection).get_columns("managed_agent")
    }
    for table, field in (
        ("flow_runner", "flow_execution_consent"),
        ("managed_agent", "others_may_start_sessions"),
    ):
        # A synthetic insert reaches the check independently of FK or NOT NULL
        # failures by evaluating the constraint expression directly.
        constraints = inspect(connection).get_check_constraints(table)
        assert any(field in constraint["sqltext"] for constraint in constraints)
        default = next(
            c["default"]
            for c in inspect(connection).get_columns(table)
            if c["name"] == field
        )
        assert "ask_me" in default


def test_runner_rejects_invalid_consent_mode(
    db_session: Session, test_user: models.User
) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO flow_runner (id, account_id, name, token_hash, labels, capabilities, publication_capabilities, status, flow_execution_consent) VALUES (gen_random_uuid(), :account, 'Example runner', 'synthetic', '[]', '{}', '{}', 'online', 'allow')"
                ),
                {"account": test_user.account_id},
            )
