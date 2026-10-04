"""Migration ``20261004_copilot_user_mapping`` (#1061): mapping table and
finding supersession columns, round-tripped through downgrade and upgrade."""

import importlib.util
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.models.base import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261004_copilot_user_mapping.py"
)
TABLES = ("copilot_user_mapping", "spend_outlier_finding")


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "copilot_user_mapping_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection, table):
    return {column["name"] for column in inspect(connection).get_columns(table)}


def test_downgrade_then_upgrade_restores_the_model_shape(db_session):
    migration = _load_migration()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert "copilot_user_mapping" not in inspect(connection).get_table_names()
        finding_columns = _columns(connection, "spend_outlier_finding")
        assert "superseded_at" not in finding_columns
        assert "superseded_reason" not in finding_columns
        migration.upgrade()

    for model in (models.CopilotUserMapping, models.SpendOutlierFinding):
        assert _columns(connection, model.__tablename__) == {
            column.name for column in model.__table__.columns
        }
    unique = {
        constraint["name"]
        for constraint in inspect(connection).get_unique_constraints(
            "copilot_user_mapping"
        )
    }
    assert "uq_copilot_user_mapping_login" in unique


def test_model_metadata_matches_the_migrated_schema(db_session):
    """Autogenerate must see nothing to add or drop on the two tables.

    The ``ix_<table>_id`` index that ``Base`` declares on every primary key is
    not created by any migration in this tree (the primary key already indexes
    the column), so that one well-known diff is ignored here as elsewhere.
    """
    context = MigrationContext.configure(db_session.connection())
    diffs = compare_metadata(context, Base.metadata)

    def touches(diff) -> bool:
        items = diff if isinstance(diff, list) else [diff]
        for item in items:
            if not isinstance(item, tuple):
                continue
            if item[0] == "add_index" and item[1].name in {
                f"ix_{table}_id" for table in TABLES
            }:
                continue
            for part in item[1:]:
                name = getattr(part, "name", None)
                table = getattr(part, "table", None)
                if name in TABLES or getattr(table, "name", None) in TABLES:
                    return True
                if isinstance(part, str) and part in TABLES:
                    return True
        return False

    assert [diff for diff in diffs if touches(diff)] == []


def test_one_login_maps_to_one_user_per_account_and_organization(db_session, test_user):
    """The unique key refuses a second row for the same login; another
    organization or another account may reuse the login."""
    from preloop.models.crud import crud_account, crud_copilot_import_connection
    from preloop.services.copilot_usage_import import COPILOT_IMPORT_SECRET_KIND
    from preloop.services.secret_service import get_secret_service

    def connection_for(account_id):
        secret = get_secret_service().create_local_secret_reference(
            db_session,
            account_id=account_id,
            name="org",
            secret_kind=COPILOT_IMPORT_SECRET_KIND,
            secret_value="unused",
        )
        return crud_copilot_import_connection.create(
            db_session,
            obj_in={
                "account_id": account_id,
                "organization": "example-org",
                "secret_reference_id": secret.id,
            },
        )

    def insert(account_id, connection_id, organization, user_id):
        db_session.execute(
            models.CopilotUserMapping.__table__.insert().values(
                id=uuid.uuid4(),
                account_id=account_id,
                connection_id=connection_id,
                organization=organization,
                github_login="alice",
                user_id=user_id,
            )
        )

    connection = connection_for(test_user.account_id)
    insert(test_user.account_id, connection.id, "example-org", test_user.id)
    insert(test_user.account_id, connection.id, "other-org", test_user.id)

    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    other_user = models.User(
        account_id=other_account.id,
        email="other@other.example.com",
        username="other",
        is_active=True,
        hashed_password="x",
        user_source="local",
    )
    db_session.add(other_user)
    db_session.flush()
    other_connection = connection_for(other_account.id)
    insert(other_account.id, other_connection.id, "example-org", other_user.id)

    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            insert(test_user.account_id, connection.id, "example-org", test_user.id)

    assert db_session.query(models.CopilotUserMapping).count() == 3


def test_supersession_columns_are_nullable_and_independent_of_dismissal(
    db_session, test_user
):
    finding = models.SpendOutlierFinding(
        account_id=test_user.account_id,
        rule="daily_spend",
        user_id=test_user.id,
        day=date(2026, 9, 24),
        item_id=f"spend:daily_spend:{test_user.id}",
        fingerprint=f"daily_spend|{test_user.id}|2026-09-24",
        details={},
        detected_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
    )
    db_session.add(finding)
    db_session.flush()
    assert finding.superseded_at is None
    assert finding.superseded_reason is None

    db_session.execute(
        text(
            "UPDATE spend_outlier_finding SET superseded_at = :at, "
            "superseded_reason = 'no_longer_qualifies' WHERE id = :id"
        ),
        {"at": datetime(2026, 9, 28, tzinfo=timezone.utc), "id": finding.id},
    )
    db_session.refresh(finding)
    assert finding.superseded_reason == "no_longer_qualifies"
    assert finding.dismissed_at is None
