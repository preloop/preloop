"""Mapping provenance preserves manual grants and overlapping identity sources."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_role,
    crud_team,
    crud_user_role,
)
from preloop.models.crud import sso_group_grants as grants


@pytest.fixture
def setup_grants(db_session: Session, test_user: models.User) -> tuple:
    connection = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=test_user.account_id,
        protocol="oidc",
        fields={"name": "Work IdP"},
    )
    role = crud_role.create(
        db_session,
        obj_in={
            "name": f"mapped-{uuid4().hex}",
            "account_id": test_user.account_id,
            "is_system_role": False,
        },
    )
    team = crud_team.create(
        db_session,
        obj_in={"name": "Engineering", "account_id": test_user.account_id},
    )
    return test_user, connection, role, team


def reconcile(db: Session, setup: tuple, **kwargs: object) -> grants.GrantDelta:
    user, connection, _, _ = setup
    return grants.reconcile_grants(
        db,
        account_id=user.account_id,
        connection_id=connection.id,
        membership_id=user.id,
        source="oidc",
        **kwargs,
    )


def has_role(db: Session, user: models.User, role: models.Role) -> bool:
    return any(
        row.role_id == role.id
        for row in crud_user_role.get_by_user(db, user_id=user.id)
    )


def test_add_then_remove_mapping_created_role_and_team(
    db_session: Session, setup_grants: tuple
) -> None:
    user, _, role, team = setup_grants
    delta = reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    assert delta.roles_added == (role.id,) and delta.teams_added == (team.id,)
    assert has_role(db_session, user, role)
    membership = crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)
    assert membership is not None and not membership.manually_granted
    delta = reconcile(db_session, setup_grants)
    assert delta.roles_removed == (role.id,) and delta.teams_removed == (team.id,)
    assert not has_role(db_session, user, role)
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=user.id) is None
    )


def test_manual_grants_survive_mapping_overlap_and_removal(
    db_session: Session, setup_grants: tuple
) -> None:
    user, _, role, team = setup_grants
    crud_user_role.assign_role(db_session, user_id=user.id, role_id=role.id)
    crud_team.add_member(db_session, team_id=team.id, user_id=user.id)
    reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    reconcile(db_session, setup_grants)
    assert has_role(db_session, user, role)
    assert crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)


def test_two_connections_and_scim_retain_independent_claims(
    db_session: Session, setup_grants: tuple
) -> None:
    user, connection, role, team = setup_grants
    other = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=user.account_id,
        protocol="saml",
        fields={"name": "Other IdP"},
    )
    reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    for target_connection, source in [(other, "saml"), (connection, "scim")]:
        grants.reconcile_grants(
            db_session,
            account_id=user.account_id,
            connection_id=target_connection.id,
            membership_id=user.id,
            source=source,
            role_ids={role.id},
            team_ids={team.id},
        )
    reconcile(db_session, setup_grants)
    grants.reconcile_grants(
        db_session,
        account_id=user.account_id,
        connection_id=other.id,
        membership_id=user.id,
        source="saml",
    )
    assert has_role(db_session, user, role)
    assert crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)
    grants.reconcile_grants(
        db_session,
        account_id=user.account_id,
        connection_id=connection.id,
        membership_id=user.id,
        source="scim",
    )
    assert not has_role(db_session, user, role)
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=user.id) is None
    )


def test_manual_replacement_is_not_deleted_by_old_mapping_claims(
    db_session: Session, setup_grants: tuple
) -> None:
    user, _, role, team = setup_grants
    reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    crud_user_role.remove_role(db_session, user_id=user.id, role_id=role.id)
    crud_team.remove_member(db_session, team_id=team.id, user_id=user.id)
    crud_user_role.assign_role(db_session, user_id=user.id, role_id=role.id)
    crud_team.add_member(db_session, team_id=team.id, user_id=user.id)
    reconcile(db_session, setup_grants)
    assert has_role(db_session, user, role)
    assert crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)


def test_manual_team_edit_preserves_membership_after_group_removal(
    db_session: Session, setup_grants: tuple
) -> None:
    user, _, _, team = setup_grants
    reconcile(db_session, setup_grants, team_ids={team.id})
    membership = crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)
    crud_team.update_member(db_session, membership=membership, role="lead")
    reconcile(db_session, setup_grants)
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=user.id).role
        == "lead"
    )


@pytest.mark.parametrize("foreign_target", ["role", "team", "connection"])
def test_cross_account_references_are_rejected_before_writes(
    db_session: Session, setup_grants: tuple, foreign_target: str
) -> None:
    user, connection, _, _ = setup_grants
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other account", "is_active": True}
    )
    kwargs = {}
    if foreign_target == "role":
        row = crud_role.create(
            db_session,
            obj_in={"name": "Other role", "account_id": other.id},
        )
        kwargs["role_ids"] = {row.id}
    elif foreign_target == "team":
        row = crud_team.create(
            db_session, obj_in={"name": "Other team", "account_id": other.id}
        )
        kwargs["team_ids"] = {row.id}
    else:
        connection = crud_account_sso_connection.create_for_account(
            db_session,
            account_id=other.id,
            protocol="oidc",
            fields={"name": "Other IdP"},
        )
    with pytest.raises(grants.GrantValidationError):
        grants.reconcile_grants(
            db_session,
            account_id=user.account_id,
            connection_id=connection.id,
            membership_id=user.id,
            source="oidc",
            **kwargs,
        )


def test_connection_delete_revokes_only_its_managed_access(
    db_session: Session, setup_grants: tuple
) -> None:
    user, connection, role, team = setup_grants
    reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    crud_team.update_member(
        db_session,
        membership=crud_team.get_membership(
            db_session, team_id=team.id, user_id=user.id
        ),
        role="lead",
    )
    crud_account_sso_connection.delete(
        db_session, id=connection.id, account_id=user.account_id
    )
    assert not has_role(db_session, user, role)
    assert crud_team.get_membership(db_session, team_id=team.id, user_id=user.id)


@pytest.mark.parametrize("target", ["mapping", "default"])
@pytest.mark.parametrize("owner_name", ["owner", "OWNER", " owner "])
def test_database_rejects_owner_aliases_at_write_time(
    db_session: Session, setup_grants: tuple, target: str, owner_name: str
) -> None:
    from sqlalchemy.exc import IntegrityError
    from preloop.models.crud import crud_sso_group_mapping

    user, connection, _, _ = setup_grants
    with pytest.raises(IntegrityError), db_session.begin_nested():
        if target == "mapping":
            crud_sso_group_mapping.create_mapping(
                db_session,
                account_id=user.account_id,
                connection_id=connection.id,
                idp_group="owners",
                role=owner_name,
            )
        else:
            crud_account_sso_connection.apply_update(
                db_session,
                connection=connection,
                fields={"default_role": owner_name},
            )


def test_reconciliation_changes_rollback_with_caller_transaction(
    db_session: Session,
    setup_grants: tuple,
) -> None:
    user, _, role, team = setup_grants
    with pytest.raises(RuntimeError), db_session.begin_nested():
        reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
        raise RuntimeError("simulate later login audit/token failure")
    assert not has_role(db_session, user, role)
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=user.id) is None
    )


def test_migration_preserves_existing_grants_as_manual(
    db_session: Session,
    setup_grants: tuple,
) -> None:
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    user, _, role, team = setup_grants
    reconcile(db_session, setup_grants, role_ids={role.id}, team_ids={team.id})
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261114_sso_group_grants.py"
    )
    spec = importlib.util.spec_from_file_location("group_grants_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        assert "manually_granted" not in {
            column["name"]
            for column in inspect(db_session.connection()).get_columns("user_role")
        }
        migration.upgrade()
    db_session.expire_all()
    assert has_role(db_session, user, role)
    assert all(
        row.manually_granted
        for row in crud_user_role.get_by_user(db_session, user_id=user.id)
    )
    assert crud_team.get_membership(
        db_session, team_id=team.id, user_id=user.id
    ).manually_granted


@pytest.mark.parametrize("alias", ["Owner", " owner ", "OWNER"])
def test_migration_normalizes_legacy_owner_aliases(
    db_session: Session,
    setup_grants: tuple,
    alias: str,
) -> None:
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from preloop.models.crud import crud_sso_group_mapping

    user, connection, _, _ = setup_grants
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261114_sso_group_grants.py"
    )
    spec = importlib.util.spec_from_file_location("group_grants_legacy_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        crud_account_sso_connection.apply_update(
            db_session,
            connection=connection,
            fields={"default_role": alias},
        )
        mapping = crud_sso_group_mapping.create_mapping(
            db_session,
            account_id=user.account_id,
            connection_id=connection.id,
            idp_group="legacy-admin",
            role=alias,
        )
        migration.upgrade()
    db_session.refresh(connection)
    db_session.refresh(mapping)
    assert connection.default_role == "member"
    assert mapping.role == "member"


@pytest.mark.parametrize(
    "permission_name", ["manage_billing", "close_account", "new_owner_privilege"]
)
def test_owner_only_permissions_are_rejected_in_core(
    db_session: Session,
    setup_grants: tuple,
    permission_name: str,
) -> None:
    from preloop.models.crud import crud_permission

    user, _, role, _ = setup_grants
    permission = crud_permission.get_by_name(db_session, name=permission_name)
    if permission is None:
        permission = crud_permission.create(
            db_session,
            obj_in={
                "name": permission_name,
                "category": "account",
                "description": "Synthetic owner-only permission",
            },
        )
    if permission_name == "new_owner_privilege":
        owner = crud_role.get_by_name(db_session, name="owner") or crud_role.create(
            db_session, obj_in={"name": "owner", "is_system_role": True}
        )
        crud_role.get_by_name(db_session, name="admin") or crud_role.create(
            db_session, obj_in={"name": "admin", "is_system_role": True}
        )
        crud_role.assign_permission(
            db_session, role_id=owner.id, permission_id=permission.id
        )
    crud_role.assign_permission(
        db_session, role_id=role.id, permission_id=permission.id
    )
    with pytest.raises(grants.GrantValidationError):
        reconcile(db_session, setup_grants, role_ids={role.id})
    assert not has_role(db_session, user, role)


def test_team_inheriting_owner_is_rejected_in_core(
    db_session: Session,
    setup_grants: tuple,
) -> None:
    from preloop.models.crud import crud_team_role

    user, _, _, team = setup_grants
    owner = crud_role.get_by_name(db_session, name="owner") or crud_role.create(
        db_session, obj_in={"name": "owner", "is_system_role": True}
    )
    crud_team_role.assign_role(db_session, team_id=team.id, role_id=owner.id)
    with pytest.raises(grants.GrantValidationError):
        reconcile(db_session, setup_grants, team_ids={team.id})
    assert (
        crud_team.get_membership(db_session, team_id=team.id, user_id=user.id) is None
    )


def test_reserved_member_resolves_only_genuine_system_viewer(
    db_session: Session,
    setup_grants: tuple,
) -> None:
    user, _, _, _ = setup_grants
    viewer = crud_role.get_by_name(db_session, name="viewer") or crud_role.create(
        db_session, obj_in={"name": "viewer", "is_system_role": True}
    )
    for name in ("member", "viewer"):
        crud_role.create(
            db_session, obj_in={"name": name, "account_id": user.account_id}
        )
    assert (
        grants.resolve_role(db_session, account_id=user.account_id, name="member").id
        == viewer.id
    )


@pytest.mark.parametrize("winner", ["removal", "edit"])
def test_concurrent_manual_team_edit_and_reconciliation(
    db_engine: object,
    winner: str,
) -> None:
    """Both race outcomes are clean; a completed edit always remains manual."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event
    from preloop.models.crud import crud_user
    from preloop.models.crud.base import CRUDBase

    cached = Event()
    mapper_cached = Event()
    removed = Event()
    edit_select = Event()
    edited = Event()
    with Session(db_engine) as setup:
        account = crud_account.create(
            setup, obj_in={"organization_name": "Concurrent mapping", "is_active": True}
        )
        user = crud_user.create(
            setup,
            obj_in={
                "account_id": account.id,
                "username": f"concurrent-{uuid4().hex}",
                "email": f"{uuid4().hex}@example.test",
                "hashed_password": "synthetic",
                "is_active": True,
                "user_source": "local",
            },
        )
        team = crud_team.create(
            setup, obj_in={"name": "Race", "account_id": account.id}
        )
        connection = crud_account_sso_connection.create_for_account(
            setup, account_id=account.id, protocol="oidc", fields={"name": "Race IdP"}
        )
        grants.reconcile_grants(
            setup,
            account_id=account.id,
            connection_id=connection.id,
            membership_id=user.id,
            source="oidc",
            team_ids={team.id},
        )
        setup.commit()
        account_id, person_id, user_id, team_id, connection_id = (
            account.id,
            user.person_id,
            user.id,
            team.id,
            connection.id,
        )

    def remove() -> None:
        with Session(db_engine) as db:
            # Cache the managed row before the manual editor commits.
            before = crud_team.get_membership(db, team_id=team_id, user_id=user_id)
            assert before is not None and not before.manually_granted
            mapper_cached.set()
            assert cached.wait(10)
            if winner == "edit":
                assert edited.wait(10)
            grants.reconcile_grants(
                db,
                account_id=account_id,
                connection_id=connection_id,
                membership_id=user_id,
                source="oidc",
            )
            removed.set()
            if winner == "removal":
                assert edit_select.wait(10)
            db.commit()

    def edit() -> str | None:
        with Session(db_engine) as db:
            membership = crud_team.get_membership(db, team_id=team_id, user_id=user_id)
            assert membership is not None
            cached.set()
            assert mapper_cached.wait(10)
            if winner == "removal":
                assert removed.wait(10)

            def selected(*args: object) -> None:
                statement = str(args[2]).lstrip()
                if "FOR UPDATE" in statement or statement.startswith(
                    "UPDATE team_membership"
                ):
                    edit_select.set()

            event.listen(db.connection(), "before_cursor_execute", selected)
            result = crud_team.update_member(db, membership=membership, role="lead")
            edited.set()
            return result.role if result else None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            removal = executor.submit(remove)
            editor = executor.submit(edit)
            removal.result(timeout=20)
            result = editor.result(timeout=20)
        with Session(db_engine) as verify:
            membership = crud_team.get_membership(
                verify, team_id=team_id, user_id=user_id
            )
            if winner == "removal":
                assert result is None and membership is None
            else:
                assert result == "lead" and membership is not None
                assert membership.manually_granted and membership.role == "lead"
    finally:
        # Independent connections require committed fixtures; remove them explicitly.
        with Session(db_engine) as cleanup:
            crud_account.delete(cleanup, id=account_id)
            if person_id:
                CRUDBase(models.Person).delete(cleanup, id=person_id)


def test_owner_catalog_is_loaded_once_for_a_complete_snapshot(
    db_session: Session,
    setup_grants: tuple,
) -> None:
    from unittest.mock import patch
    from preloop.models.crud import crud_team_role

    user, _, role, team = setup_grants
    other = crud_role.create(
        db_session,
        obj_in={"name": f"extra-{uuid4().hex}", "account_id": user.account_id},
    )
    crud_team_role.assign_role(db_session, team_id=team.id, role_id=other.id)
    with patch.object(crud_role, "get_by_name", wraps=crud_role.get_by_name) as lookup:
        reconcile(
            db_session, setup_grants, role_ids={role.id, other.id}, team_ids={team.id}
        )
    names = [call.kwargs["name"] for call in lookup.call_args_list]
    assert names.count("owner") == names.count("admin") == 1
