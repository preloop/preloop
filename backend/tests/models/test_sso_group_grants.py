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
