"""Run the event webhook migration's downgrade/upgrade cycle on real Postgres.

`test_alembic_single_head.py` only walks the revision graph, and the test
database is built from ORM metadata, so neither one ever executes this
migration's DDL. That leaves two failure modes invisible until deploy: a
column the ORM has and the migration forgot (or vice versa), and a
downgrade that cannot run because the drops are ordered against the foreign
key. These tests run downgrade() then upgrade() inside the test transaction
and diff the rebuilt tables against the ORM definition.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models.models.webhook_endpoint import WebhookDelivery, WebhookEndpoint

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260908_event_webhooks.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "event_webhooks_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(db_session):
    """An Alembic operations context bound to the test transaction."""
    context = MigrationContext.configure(db_session.connection())
    return Operations.context(context)


def _column_names(db_session, table: str) -> set[str]:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(table)
    }


def test_downgrade_drops_both_tables(db_session):
    """Drops are ordered so the outbox goes before the endpoints it points at."""
    migration = _load_migration()
    inspector = inspect(db_session.connection())
    assert inspector.has_table("webhook_endpoint")
    assert inspector.has_table("webhook_delivery")

    with _operations(db_session):
        migration.downgrade()

    inspector = inspect(db_session.connection())
    assert not inspector.has_table("webhook_delivery")
    assert not inspector.has_table("webhook_endpoint")


def test_upgrade_after_downgrade_rebuilds_the_orm_shape(db_session):
    """A full down/up cycle leaves exactly the columns the ORM expects."""
    migration = _load_migration()
    expected_endpoint = {c.name for c in WebhookEndpoint.__table__.columns}
    expected_delivery = {c.name for c in WebhookDelivery.__table__.columns}

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    assert _column_names(db_session, "webhook_endpoint") == expected_endpoint
    assert _column_names(db_session, "webhook_delivery") == expected_delivery


def test_upgrade_recreates_the_indexes_and_the_idempotency_constraint(db_session):
    """The unique key is what makes emitting the same fact twice harmless."""
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    inspector = inspect(db_session.connection())
    endpoint_indexes = {i["name"] for i in inspector.get_indexes("webhook_endpoint")}
    delivery_indexes = {i["name"] for i in inspector.get_indexes("webhook_delivery")}
    unique = {
        c["name"]: c["column_names"]
        for c in inspector.get_unique_constraints("webhook_delivery")
    }

    assert "ix_webhook_endpoint_account_id" in endpoint_indexes
    assert "ix_webhook_endpoint_approval_workflow_id" in endpoint_indexes
    assert {
        "ix_webhook_delivery_account_id",
        "ix_webhook_delivery_endpoint_id",
        "ix_webhook_delivery_due",
        "ix_webhook_delivery_account_status",
    } <= delivery_indexes
    assert unique["uq_webhook_delivery_event"] == [
        "endpoint_id",
        "event_id",
        "generation",
    ]


def test_upgrade_keeps_the_cascades_that_stop_orphan_deliveries(db_session):
    """Deleting an account or an endpoint must take its outbox rows with it."""
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    inspector = inspect(db_session.connection())
    rules = {
        tuple(fk["constrained_columns"]): fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("webhook_delivery")
    }
    endpoint_rules = {
        tuple(fk["constrained_columns"]): fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("webhook_endpoint")
    }

    assert rules[("account_id",)] == "CASCADE"
    assert rules[("endpoint_id",)] == "CASCADE"
    assert endpoint_rules[("account_id",)] == "CASCADE"
    assert endpoint_rules[("approval_workflow_id",)] == "CASCADE"
    # A deleted operator must not take the endpoint they registered with them.
    assert endpoint_rules[("created_by_user_id",)] == "SET NULL"
