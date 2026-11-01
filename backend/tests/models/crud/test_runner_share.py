"""Runner share grant and owner consent policy CRUD (EE #186)."""

import secrets
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.crud import crud_runner_share, crud_team, crud_user


def _user(db, account_id, name):
    return crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": f"{name}-{uuid4().hex[:6]}@example.com",
            "username": f"{name}-{uuid4().hex[:6]}",
            "full_name": name,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def _runner(db, owner):
    runner = models.FlowRunner(
        account_id=owner.account_id,
        registered_by_user_id=owner.id,
        name="laptop",
        hostname="laptop.example.com",
        token_hash=secrets.token_hex(32),
        status="online",
    )
    db.add(runner)
    db.flush()
    return runner


def _grant(db, runner, owner, grantee_type, grantee_id, **kw):
    values = dict(
        account_id=runner.account_id,
        runner_id=runner.id,
        owner_user_id=owner.id,
        grantee_type=grantee_type,
        grantee_id=grantee_id,
        scopes=["sessions"],
        harnesses=["claude_code"],
        directory_ids=["dir_d"],
        consent_state="active",
        consent_source="owner",
        created_by_user_id=owner.id,
        commit=False,
    )
    values.update(kw)
    return crud_runner_share.create_grant(db, **values)


def test_active_grants_match_user_and_team(db_session, test_user):
    runner = _runner(db_session, test_user)
    member = _user(db_session, test_user.account_id, "member")
    outsider = _user(db_session, test_user.account_id, "outsider")
    team = models.Team(account_id=test_user.account_id, name=f"t-{uuid4().hex[:6]}")
    db_session.add(team)
    db_session.flush()
    crud_team.add_member(db_session, team_id=team.id, user_id=member.id)
    team_grant = _grant(db_session, runner, test_user, "team", team.id)
    _grant(
        db_session,
        runner,
        test_user,
        "user",
        member.id,
        consent_state="pending_owner_consent",
        consent_source=None,
    )
    teams = crud_runner_share.team_ids_for_user(db_session, user_id=member.id)
    assert teams == [team.id]
    found = crud_runner_share.active_grants_for_user(
        db_session,
        account_id=test_user.account_id,
        runner_id=runner.id,
        user_id=member.id,
        team_ids=teams,
    )
    assert [g.id for g in found] == [team_grant.id]
    assert team_grant.consented_at is not None
    assert (
        crud_runner_share.active_grants_for_user(
            db_session,
            account_id=test_user.account_id,
            runner_id=runner.id,
            user_id=outsider.id,
        )
        == []
    )
    crud_runner_share.revoke(
        db_session, team_grant, revoked_by_user_id=test_user.id, commit=False
    )
    assert (
        crud_runner_share.active_grants_for_user(
            db_session,
            account_id=test_user.account_id,
            runner_id=runner.id,
            user_id=member.id,
            team_ids=teams,
        )
        == []
    )
    assert (
        len(
            crud_runner_share.list_for_runner(
                db_session,
                account_id=test_user.account_id,
                runner_id=runner.id,
                include_revoked=True,
            )
        )
        == 2
    )


def test_scopes_are_checked_by_the_database(db_session, test_user):
    runner = _runner(db_session, test_user)
    with pytest.raises(IntegrityError):
        _grant(db_session, runner, test_user, "user", test_user.id, scopes=["admin"])


def test_policy_set_and_clear(db_session, test_user):
    runner = _runner(db_session, test_user)
    account = test_user.account_id
    assert crud_runner_share.get_policy(db_session, account_id=account) is None
    crud_runner_share.set_policy(
        db_session,
        account_id=account,
        runner_id=None,
        owner_consent="not_required",
        updated_by_user_id=test_user.id,
        commit=False,
    )
    crud_runner_share.set_policy(
        db_session,
        account_id=account,
        runner_id=runner.id,
        owner_consent="required",
        updated_by_user_id=test_user.id,
        commit=False,
    )
    assert (
        crud_runner_share.get_policy(db_session, account_id=account).owner_consent
        == "not_required"
    )
    assert (
        crud_runner_share.get_policy(
            db_session, account_id=account, runner_id=runner.id
        ).owner_consent
        == "required"
    )
    crud_runner_share.set_policy(
        db_session,
        account_id=account,
        runner_id=runner.id,
        owner_consent=None,
        updated_by_user_id=test_user.id,
        commit=False,
    )
    assert (
        crud_runner_share.get_policy(
            db_session, account_id=account, runner_id=runner.id
        )
        is None
    )
