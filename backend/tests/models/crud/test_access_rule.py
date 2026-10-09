"""PostgreSQL account isolation, snapshots and committed invalidation for ABAC."""

from typing import Any

import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.access_rule import CRUDAccessRule, AccessRuleConflictError
from preloop.schemas.access_rule import AccessRuleDefinition


@pytest.fixture
def storage(request: pytest.FixtureRequest) -> Any:
    url = make_url(os.environ["DATABASE_URL"])
    if hasattr(request, "param"):
        url = url.set(drivername=f"postgresql+{request.param}")
    engine = create_engine(url)
    ids = []
    with Session(engine) as db, db.begin():
        for _ in range(2):
            account = models.Account(organization_name="Synthetic ABAC tenant")
            db.add(account)
            db.flush()
            account.hierarchy_path = [account.id]
            user = models.User(
                account_id=account.id,
                username=str(uuid4()),
                email=f"{uuid4()}@example.com",
                hashed_password="synthetic",
            )
            db.add(user)
            db.flush()
            ids.append((account.id, user.id))
    try:
        yield CRUDAccessRule(engine), ids
    finally:
        with engine.begin() as conn:
            for model in (models.PolicySnapshot, models.AuditLog):
                conn.execute(
                    delete(model).where(model.account_id.in_([key for key, _ in ids]))
                )
            conn.execute(
                delete(models.Account).where(
                    models.Account.id.in_([key for key, _ in ids])
                )
            )
        engine.dispose()


def definition(**values: Any) -> Any:
    return AccessRuleDefinition(
        name="Synthetic rule", effect="forbid", actions=["tool:call"], **values
    )


def test_crud_account_isolation_versions_snapshot_audit(storage: Any) -> None:
    crud, owners = storage
    account, user = owners[0]
    before = crud.generation(account_id=account)
    rule = crud.write(account_id=account, user_id=user, definition=definition())
    assert rule["version"] == 1
    assert crud.generation(account_id=account) > before
    assert crud.rules(account_id=owners[1][0]) == []
    with pytest.raises(AccessRuleConflictError):
        crud.delete(
            account_id=owners[1][0],
            user_id=owners[1][1],
            rule_id=rule["id"],
            expected_version=1,
        )
    with pytest.raises(AccessRuleConflictError):
        crud.write(
            account_id=account,
            user_id=user,
            rule_id=rule["id"],
            expected_version=99,
            definition=definition(),
        )
    changed = crud.write(
        account_id=account,
        user_id=user,
        rule_id=rule["id"],
        expected_version=1,
        definition=definition(),
    )
    assert changed["version"] == 2
    with crud.session() as db:
        snapshots = db.scalars(
            select(models.PolicySnapshot).where(
                models.PolicySnapshot.account_id == account
            )
        ).all()
        assert len(snapshots) == 2
        assert snapshots[-1].snapshot_data["access_rules"][0]["id"] == str(rule["id"])
        audits = db.scalars(
            select(models.AuditLog).where(models.AuditLog.account_id == account)
        ).all()
        assert len(audits) == 2
    crud.delete(
        account_id=account, user_id=user, rule_id=rule["id"], expected_version=2
    )
    assert crud.rules(account_id=account) == []


def test_mode_generation_replay_and_bundle_secret_boundary(storage: Any) -> None:
    crud, owners = storage
    account, user = owners[0]
    generation = crud.generation(account_id=account)
    crud.set_mode(
        account_id=account,
        user_id=user,
        action="tool:call",
        mode="require_permit",
        expected_generation=generation,
        losing_subjects=[],
    )
    with pytest.raises(AccessRuleConflictError):
        crud.set_mode(
            account_id=account,
            user_id=user,
            action="tool:call",
            mode="require_permit",
            expected_generation=generation,
            losing_subjects=[],
        )
    bundle = crud.bundle(account_id=account)
    assert bundle["modes"]["tool:call"] == "require_permit"
    subject = bundle["subjects"][("user", str(user))]
    assert "hashed_password" not in subject
    assert "email" not in subject


@pytest.mark.parametrize("storage", ["psycopg", "psycopg2"], indirect=True)
def test_listener_receives_committed_changes_only(storage: Any) -> None:
    crud, owners = storage
    account, user = owners[0]
    ready, stop, received = threading.Event(), threading.Event(), threading.Event()
    events = []

    def changed(key: Any, generation: Any) -> None:
        events.append((key, generation))
        if key == str(account):
            received.set()

    thread = threading.Thread(
        target=crud.listen, args=(changed, ready, stop), daemon=True
    )
    thread.start()
    try:
        assert ready.wait(5)
        with crud.session() as db:
            row = models.AccessRule(
                account_id=account,
                created_by=user,
                name="Rolled back",
                effect="forbid",
                actions=["tool:call"],
            )
            db.add(row)
            db.flush()
            db.rollback()
        assert not received.wait(0.2)
        crud.write(account_id=account, user_id=user, definition=definition())
        assert received.wait(5)
        assert events[-1][1] == crud.generation(account_id=account)
    finally:
        stop.set()
        thread.join(3)
    assert not thread.is_alive()


def test_parent_rules_are_inherited_readonly_and_parent_mode_is_ceiling(
    storage: Any,
) -> None:
    crud, owners = storage
    parent, user = owners[0]
    child, child_user = owners[1]
    with crud.session() as db, db.begin():
        row = db.get(models.Account, child)
        row.parent_account_id = parent
        row.hierarchy_path = [parent, child]
        row.hierarchy_depth = 1
        row.root_account_id = parent
    rule = crud.write(
        account_id=parent, user_id=user, definition=definition(scope="subaccounts")
    )
    inherited = crud.rules(account_id=child)
    assert inherited[0]["id"] == rule["id"]
    assert inherited[0]["inherited"] and not inherited[0]["editable"]
    assert crud.bundle(account_id=parent)["rules"] == []
    assert crud.bundle(account_id=child)["rules"][0]["account_id"] == str(parent)
    with pytest.raises(AccessRuleConflictError):
        crud.write(
            account_id=child,
            user_id=child_user,
            rule_id=rule["id"],
            expected_version=1,
            definition=definition(),
        )
    generation = crud.generation(account_id=child)
    crud.set_mode(
        account_id=parent,
        user_id=user,
        action="tool:call",
        mode="require_permit",
        expected_generation=crud.generation(account_id=parent),
        losing_subjects=[],
    )
    assert crud.generation(account_id=child) > generation
    with crud.session() as db, db.begin():
        db.get(models.Account, child).meta_data = {
            "access_rule_mode": {"tool:call": "additive"}
        }
    bundle = crud.bundle(account_id=child)
    assert bundle["modes"]["tool:call"] == "require_permit"
    assert bundle["ancestor_require_permit"] == ["tool:call"]


def test_moving_rule_invalidates_previous_and_new_owner(storage: Any) -> None:
    crud, owners = storage
    first, user = owners[0]
    second, _ = owners[1]
    rule = crud.write(account_id=first, user_id=user, definition=definition())
    old_generation = crud.generation(account_id=first)
    new_generation = crud.generation(account_id=second)
    with crud.session() as db, db.begin():
        db.get(models.AccessRule, rule["id"]).account_id = second
    assert crud.generation(account_id=first) > old_generation
    assert crud.generation(account_id=second) > new_generation


def test_policy_yaml_roundtrip_and_require_permit_import_guard(storage: Any) -> None:
    from preloop.models.crud.access_rule import (
        apply_policy_section,
        validate_policy_section,
    )
    from preloop.services.policy.loader import export_current_policy

    crud, owners = storage
    account, user = owners[0]
    rule = crud.write(account_id=account, user_id=user, definition=definition())
    with crud.session() as db, db.begin():
        exported = export_current_policy(db, account)
        assert exported.access_rules[0].id == rule["id"]
        apply_policy_section(db, account, user, exported.access_rules, {})
    assert crud.rules(account_id=account)[0]["version"] == 2
    with crud.session() as db:
        with pytest.raises(AccessRuleConflictError, match="preview"):
            validate_policy_section(db, account, [], {"tool:call": "require_permit"})
