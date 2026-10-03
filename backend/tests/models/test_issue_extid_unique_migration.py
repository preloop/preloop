"""Run the duplicate-issue merge migration against Postgres (#1194).

Seeds the shape concurrent webhook deliveries left behind (several rows for
one ``(project_id, external_id)``, dependents on the extras) and runs the
migration's ``upgrade()`` inside the test transaction.
"""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_issue,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.crud.base import CRUDBase

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261003_issue_extid_unique.py"
)


def _run_upgrade(db: Session) -> None:
    spec = importlib.util.spec_from_file_location("issue_extid_unique", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(db.connection())):
        module.upgrade()


def _project(db: Session) -> tuple[Any, models.Project, models.Tracker]:
    account = crud_account.create(
        db, obj_in={"organization_name": "Example", "is_active": True}
    )
    tracker = crud_tracker.create(
        db,
        obj_in={
            "name": "Example tracker",
            "tracker_type": "github",
            "account_id": account.id,
            "api_key": "fake-local-only",
        },
    )
    org = crud_organization.create(
        db,
        obj_in={"name": "example", "identifier": "example", "tracker_id": tracker.id},
    )
    project = crud_project.create(
        db,
        obj_in={"name": "project", "identifier": "1", "organization_id": org.id},
    )
    return account, project, tracker


def test_upgrade_keeps_oldest_row_and_repoints_dependents(db_session: Session) -> None:
    db = db_session
    db.execute(text("ALTER TABLE issue DROP CONSTRAINT uq_issue_project_external_id"))
    account, project, tracker = _project(db)
    base = datetime(2026, 1, 1)
    rows = [
        CRUDBase(models.Issue).create(
            db,
            obj_in={
                "title": "Saved filters disappear",
                "external_id": "9000001",
                "key": "example/project#7",
                "project_id": project.id,
                "tracker_id": tracker.id,
                "created_at": base + timedelta(seconds=offset),
            },
        )
        for offset in (2, 0, 1)
    ]
    keeper, duplicates = rows[1], [rows[0], rows[2]]
    other = CRUDBase(models.Issue).create(
        db,
        obj_in={
            "title": "Unrelated",
            "external_id": "9000002",
            "project_id": project.id,
            "tracker_id": tracker.id,
        },
    )
    comment = CRUDBase(models.Comment).create(
        db,
        obj_in={
            "body": "Reproduced locally.",
            "external_id": "c1",
            "issue_id": duplicates[0].id,
            "tracker_id": tracker.id,
        },
    )
    child = CRUDBase(models.Issue).create(
        db,
        obj_in={
            "title": "Child",
            "external_id": "9000003",
            "project_id": project.id,
            "tracker_id": tracker.id,
            "parent_id": duplicates[1].id,
        },
    )
    lifecycle = {"account_id": account.id, "kind": "triage", "revision": "r1"}
    kept_row = CRUDBase(models.IssueLifecycle).create(
        db, obj_in={**lifecycle, "issue_id": keeper.id}
    )
    clashing = CRUDBase(models.IssueLifecycle).create(
        db, obj_in={**lifecycle, "issue_id": duplicates[0].id}
    )
    moved = CRUDBase(models.IssueLifecycle).create(
        db, obj_in={**lifecycle, "revision": "r2", "issue_id": duplicates[1].id}
    )
    ids = {
        "project": project.id,
        "tracker": tracker.id,
        "keeper": keeper.id,
        "dups": [d.id for d in duplicates],
        "other": other.id,
        "comment": comment.id,
        "child": child.id,
        "kept": kept_row.id,
        "clashing": clashing.id,
        "moved": moved.id,
    }
    db.flush()
    db.expunge_all()

    _run_upgrade(db)

    stored = db.scalars(
        select(models.Issue.id).where(models.Issue.external_id == "9000001")
    ).all()
    assert stored == [ids["keeper"]]
    assert db.get(models.Issue, ids["other"]) is not None
    assert db.get(models.Comment, ids["comment"]).issue_id == ids["keeper"]
    assert db.get(models.Issue, ids["child"]).parent_id == ids["keeper"]
    assert db.get(models.IssueLifecycle, ids["kept"]).issue_id == ids["keeper"]
    assert db.get(models.IssueLifecycle, ids["clashing"]) is None
    assert db.get(models.IssueLifecycle, ids["moved"]).issue_id == ids["keeper"]

    with pytest.raises(IntegrityError), db.begin_nested():
        CRUDBase(models.Issue).create(
            db,
            obj_in={
                "title": "Another copy",
                "external_id": "9000001",
                "project_id": ids["project"],
                "tracker_id": ids["tracker"],
            },
            commit=False,
        )


def test_upsert_updates_the_existing_row(db_session: Session) -> None:
    _, project, tracker = _project(db_session)
    values = {
        "title": "First title",
        "external_id": 9000001,
        "key": "example/project#7",
        "project_id": project.id,
        "tracker_id": tracker.id,
        "meta_data": {"labels": [], "preloop_triage": {"forged": True}},
    }
    first, created = crud_issue.upsert(db_session, obj_in=values)
    assert created and first.external_id == "9000001"
    assert "preloop_triage" not in first.meta_data
    second, created_again = crud_issue.upsert(
        db_session, obj_in={**values, "title": "Edited title", "comments": []}
    )
    assert not created_again and second.id == first.id
    assert second.title == "Edited title"
