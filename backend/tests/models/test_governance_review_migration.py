"""Upgrade and downgrade the governance review migration on empty and seeded data."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.models.models.governance_review import (
    GovernanceFinding,
    GovernancePolicyDocument,
    GovernanceReviewRun,
    GovernanceReviewSettings,
    GovernanceSessionDigest,
)
from preloop.models.models.user import User

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261016_governance_review.py"
)
THRESHOLDS_MIGRATION_PATH = MIGRATION_PATH.with_name(
    "20261019_gov_detector_thresholds.py"
)

TABLES = {
    "governance_policy_document": GovernancePolicyDocument,
    "governance_review_settings": GovernanceReviewSettings,
    "governance_session_digest": GovernanceSessionDigest,
    "governance_review_run": GovernanceReviewRun,
    "governance_finding": GovernanceFinding,
}


def _load_migration(path: Path = MIGRATION_PATH) -> ModuleType:
    """Import a migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        f"governance_review_migration_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(db_session: Session) -> Any:
    """An Alembic operations context bound to the test transaction."""
    context = MigrationContext.configure(db_session.connection())
    return Operations.context(context)


def _column_names(db_session: Session, table: str) -> set[str]:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(table)
    }


def _ensure_upgraded(db_session: Session, migration: Any) -> None:
    if not inspect(db_session.connection()).has_table("governance_finding"):
        with _operations(db_session):
            migration.upgrade()


def test_downgrade_upgrade_on_empty_database_matches_the_orm(
    db_session: Session,
) -> None:
    """A down/up cycle on tables with no rows restores the ORM columns."""
    migration = _load_migration()
    _ensure_upgraded(db_session, migration)

    with _operations(db_session):
        migration.downgrade()
        for table in TABLES:
            assert not inspect(db_session.connection()).has_table(table)
        migration.upgrade()
        _load_migration(THRESHOLDS_MIGRATION_PATH).upgrade()

    for table, model in TABLES.items():
        assert _column_names(db_session, table) == {
            column.name for column in model.__table__.columns
        }


def test_downgrade_upgrade_on_seeded_database(
    db_session: Session, test_user: User
) -> None:
    """Downgrade drops populated tables, and upgrade recreates them empty."""
    from preloop.models import models
    from preloop.models.crud.governance_review import (
        crud_governance_finding,
        crud_governance_policy_document,
        crud_governance_review_run,
        crud_governance_review_settings,
        crud_governance_session_digest,
    )

    migration = _load_migration()
    _ensure_upgraded(db_session, migration)
    account_id = test_user.account_id
    document = crud_governance_policy_document.create_version(
        db_session,
        account_id=account_id,
        body_markdown="No secrets in prompts.",
        source="user",
        created_by_id=test_user.id,
        commit=False,
    )
    window_start = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=account_id,
        window_start=window_start,
        window_end=window_end,
        policy_document_id=document.id,
        mode_before="on_platform",
        commit=False,
    )
    crud_governance_review_settings.set_mode(
        db_session,
        account_id=account_id,
        mode="on_platform",
        mode_changed_by_id=test_user.id,
        last_run_id=run.id,
        commit=False,
    )
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="cli",
        session_source_id="governance-migration",
        started_at=datetime(2026, 10, 13, 10, 0),
    )
    db_session.add(session)
    db_session.flush()
    crud_governance_session_digest.upsert(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        segment_index=0,
        window_start=window_start,
        window_end=window_end,
        policy_document_version=document.version,
        user_id=test_user.id,
        digest={"intent": "Check the parser"},
        include_history=True,
        override_caller="test/governance-migration",
        override_reason="Seed historical rows for migration compatibility",
        commit=False,
    )
    crud_governance_finding.insert_findings(
        db_session,
        account_id=account_id,
        findings=[
            {
                "run_id": run.id,
                "kind": "violation",
                "category": "unapproved_tool",
                "severity": "medium",
                "title": "Unapproved tool",
                "suggestion": "Remove it from the profile.",
                "fingerprint": "week:unapproved_tool",
                "evidence": [{"excerpt": "tool call", "source": "digest"}],
                "rule_ref": "must.tools",
            }
        ],
        commit=False,
    )

    with _operations(db_session):
        migration.downgrade()
        for table in TABLES:
            assert not inspect(db_session.connection()).has_table(table)
        migration.upgrade()
        _load_migration(THRESHOLDS_MIGRATION_PATH).upgrade()

    for table, model in TABLES.items():
        assert _column_names(db_session, table) == {
            column.name for column in model.__table__.columns
        }
    assert (
        db_session.execute(text("SELECT count(*) FROM governance_finding")).scalar()
        == 0
    )


def test_upgrade_recreates_the_one_active_document_index(db_session: Session) -> None:
    """The partial unique index is what stops two active policy versions."""
    migration = _load_migration()
    _ensure_upgraded(db_session, migration)

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    indexes = {
        index["name"]: index
        for index in inspect(db_session.connection()).get_indexes(
            "governance_policy_document"
        )
    }
    active = indexes["uq_governance_policy_document_one_active"]
    assert active["unique"] is True
    assert active["column_names"] == ["account_id"]
    assert "is_active" in str(active.get("dialect_options", {}))


def test_thresholds_migration_adds_and_drops_its_column(db_session: Session) -> None:
    """The review settings column comes and goes without touching the rest."""
    migration = _load_migration()
    _ensure_upgraded(db_session, migration)
    thresholds = _load_migration(THRESHOLDS_MIGRATION_PATH)
    with _operations(db_session):
        if "detector_thresholds" not in _column_names(
            db_session, "governance_review_settings"
        ):
            thresholds.upgrade()
        thresholds.downgrade()
    assert "detector_thresholds" not in _column_names(
        db_session, "governance_review_settings"
    )
    with _operations(db_session):
        thresholds.upgrade()
    assert "detector_thresholds" in _column_names(
        db_session, "governance_review_settings"
    )
