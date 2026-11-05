"""Persisted controls commit their audit evidence in the same transaction."""

import pytest
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import entitlement_enforcement as controls


def test_changes_and_inheritance_are_audited(db_session, test_user):
    test_user.is_superuser = True
    for account_id, enabled in [
        (None, False),
        (test_user.account_id, True),
        (test_user.account_id, None),
    ]:
        controls.set_enforcement(
            db_session,
            account_id=account_id,
            enabled=enabled,
            actor=test_user,
            reason="Incident resolved",
        )
    snapshot = controls.read_snapshot(db_session)
    assert snapshot["global"] is False
    assert snapshot[str(test_user.account_id)] is None
    rows = db_session.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.resource_type == "entitlement_enforcement")
        .order_by(models.AuditLog.timestamp)
    ).all()
    assert len(rows) == 3
    assert [row.details["before"] for row in rows] == [None, None, True]
    assert [row.details["after"] for row in rows] == [False, True, None]
    assert all(
        row.user_id == test_user.id and row.details["reason"] == "Incident resolved"
        for row in rows
    )
    assert rows[0].details["scope"] == "global"


@pytest.mark.parametrize("superuser,active", [(False, True), (True, False)])
def test_only_active_platform_admin_can_write(db_session, test_user, superuser, active):
    test_user.is_superuser, test_user.is_active = superuser, active
    with pytest.raises(PermissionError):
        controls.set_enforcement(
            db_session, enabled=False, actor=test_user, reason="Incident"
        )
    assert controls.read_snapshot(db_session) == {}


def test_reason_required(db_session, test_user):
    test_user.is_superuser = True
    with pytest.raises(ValueError, match="reason"):
        controls.set_enforcement(
            db_session, enabled=False, actor=test_user, reason="   "
        )
    assert controls.read_snapshot(db_session) == {}


def test_failed_audit_rolls_back_setting(db_session, test_user, monkeypatch):
    test_user.is_superuser = True

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(controls.crud_audit_log, "log_action", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        controls.set_enforcement(
            db_session, enabled=False, actor=test_user, reason="Incident"
        )
    assert controls.read_snapshot(db_session) == {}
