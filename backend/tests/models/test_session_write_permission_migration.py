"""Existing databases receive session permission names and role grants."""

import importlib.util
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select

from preloop.models import models

NAMES = {"share_session_searches", "run_session_optimizations"}


def test_session_permission_upgrade_is_scoped_and_repeatable(db_session: Any) -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261104_session_write_permissions.py"
    )
    spec = importlib.util.spec_from_file_location("session_permission_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db_session.execute(
        delete(models.Permission).where(models.Permission.name.in_(NAMES))
    )
    account = models.Account(organization_name="Example Organization")
    db_session.add(account)
    db_session.flush()
    custom_role = models.Role(
        name="editor", account_id=account.id, is_system_role=False
    )
    db_session.add(custom_role)
    db_session.flush()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        migration.upgrade()
    permissions = db_session.scalars(
        select(models.Permission).where(models.Permission.name.in_(NAMES))
    ).all()
    assert {p.name for p in permissions} == NAMES
    for permission in permissions:
        assert permission.is_active
        assert permission.category == "sessions"
        roles = db_session.scalars(
            select(models.Role)
            .join(models.RolePermission)
            .where(models.RolePermission.permission_id == permission.id)
        ).all()
        assert len(roles) == 4
        assert {r.name for r in roles} == {"owner", "admin", "editor", "executor"}
        assert all(r.is_system_role and r.account_id is None for r in roles)
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
    assert not db_session.scalars(
        select(models.Permission).where(models.Permission.name.in_(NAMES))
    ).all()
    assert db_session.get(models.Role, custom_role.id) is not None
