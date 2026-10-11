"""Real-DB tests for the identity fields on GET /api/v1/auth/users/me.

Mobile and web surfaces answer "is this pending approval waiting for me?" by
intersecting the caller's identity with an approval workflow's
``approver_user_ids`` / ``approver_team_ids``. That needs the profile endpoint
to carry the caller's user id, account id and team ids.
"""

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from preloop.api.auth import router as auth_router
from preloop.models.crud import crud_account, crud_user
from preloop.models.models.team import Team, TeamMembership
from preloop.models.models.user import User
from preloop.plugins import account_hooks

ME_PATH = "/api/v1/auth/users/me"


def _make_team(db_session: Session, *, account_id, name: str) -> Team:
    team = Team(account_id=account_id, name=name)
    db_session.add(team)
    db_session.flush()
    return team


def _join(db_session: Session, *, team: Team, user: User) -> None:
    db_session.add(TeamMembership(team_id=team.id, user_id=user.id))
    db_session.flush()


def test_me_returns_identity_without_teams(client, test_user: User):
    """A user in no team gets id, account_id and an empty team_ids."""
    response = client.get(ME_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(test_user.id)
    assert body["account_id"] == str(test_user.account_id)
    assert body["team_ids"] == []
    # Existing fields are untouched.
    assert body["username"] == test_user.username
    assert body["email"] == test_user.email


def test_me_returns_both_team_ids(client, db_session: Session, test_user: User):
    """A user in two teams gets both team ids."""
    alpha = _make_team(db_session, account_id=test_user.account_id, name="Alpha team")
    beta = _make_team(db_session, account_id=test_user.account_id, name="Beta team")
    _join(db_session, team=alpha, user=test_user)
    _join(db_session, team=beta, user=test_user)

    body = client.get(ME_PATH).json()

    assert body["team_ids"] == [str(alpha.id), str(beta.id)]


def test_me_omits_teams_from_other_accounts(
    client, db_session: Session, test_user: User
):
    """Only teams inside the caller's account are reported."""
    mine = _make_team(db_session, account_id=test_user.account_id, name="Mine")
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _make_team(db_session, account_id=other_account.id, name="Theirs")
    _join(db_session, team=mine, user=test_user)
    _join(db_session, team=theirs, user=test_user)

    body = client.get(ME_PATH).json()

    assert body["team_ids"] == [str(mine.id)]


def test_resolve_team_ids_reads_memberships(db_session: Session, test_user: User):
    """The resolver itself returns team ids ordered by team name."""
    zulu = _make_team(db_session, account_id=test_user.account_id, name="Zulu")
    alpha = _make_team(db_session, account_id=test_user.account_id, name="Alpha")
    _join(db_session, team=zulu, user=test_user)
    _join(db_session, team=alpha, user=test_user)

    assert auth_router._resolve_team_ids(test_user, db_session) == [
        alpha.id,
        zulu.id,
    ]


def test_resolve_team_ids_soft_fails_on_db_error():
    """A broken lookup must not 500 the profile endpoint."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("db unavailable")

    assert auth_router._resolve_team_ids(MagicMock(), db) == []


def test_me_returns_default_runner_policy(client, test_user: User):
    """OSS /users/me carries optional runner policy when no provider is set."""
    account_hooks.reset_account_hooks()
    response = client.get(ME_PATH)

    assert response.status_code == 200
    policy = response.json()["runner_policy"]
    assert policy["requirement"] == "optional"
    from preloop.schemas.auth import OSS_RUNNER_CAPABILITIES, RunnerPolicy

    assert policy["capabilities"] == list(OSS_RUNNER_CAPABILITIES)
    assert RunnerPolicy().capabilities == list(OSS_RUNNER_CAPABILITIES)
    assert policy["mandated_capabilities"] == []
    assert policy["grace_until"] is None
    assert policy["can_decide"] is True
    assert policy["message"] is None
    assert account_hooks.get_runner_policy_provider() is None


def test_me_returns_provider_runner_policy(client, test_user: User):
    """A registered provider replaces the OSS default on /users/me."""

    def provider(db, account_id, user):
        assert account_id == test_user.account_id
        assert user.id == test_user.id
        return {
            "requirement": "required",
            "capabilities": ["flows", "inventory"],
            "mandated_capabilities": ["flows", "inventory"],
            "grace_until": "2026-10-20T00:00:00Z",
            "can_decide": False,
            "message": "Your organisation requires a Preloop runner on member machines",
        }

    account_hooks.register_runner_policy_provider(provider)
    try:
        response = client.get(ME_PATH)
    finally:
        account_hooks.reset_account_hooks()

    assert response.status_code == 200
    policy = response.json()["runner_policy"]
    assert policy["requirement"] == "required"
    assert policy["capabilities"] == ["flows", "inventory"]
    assert policy["mandated_capabilities"] == ["flows", "inventory"]
    assert policy["can_decide"] is False
    assert policy["grace_until"].startswith("2026-10-20")
    assert policy["message"].startswith("Your organisation requires")


def test_me_provider_none_uses_oss_default(client):
    """A provider that returns None keeps the OSS default."""
    account_hooks.register_runner_policy_provider(lambda db, account_id, user: None)
    try:
        response = client.get(ME_PATH)
    finally:
        account_hooks.reset_account_hooks()

    assert response.status_code == 200
    assert response.json()["runner_policy"]["requirement"] == "optional"
    assert response.json()["runner_policy"]["can_decide"] is True


def test_me_invalid_provider_falls_back(client):
    """A provider mapping that fails the schema does not 500 /users/me."""

    def provider(db, account_id, user):
        return {"requirement": "sometimes"}

    account_hooks.register_runner_policy_provider(provider)
    try:
        response = client.get(ME_PATH)
    finally:
        account_hooks.reset_account_hooks()

    assert response.status_code == 200
    assert response.json()["runner_policy"]["requirement"] == "optional"


def test_me_provider_error_falls_back(client):
    """A provider that raises does not 500 /users/me."""

    def provider(db, account_id, user):
        raise RuntimeError("policy down")

    account_hooks.register_runner_policy_provider(provider)
    try:
        response = client.get(ME_PATH)
    finally:
        account_hooks.reset_account_hooks()

    assert response.status_code == 200
    assert response.json()["runner_policy"]["requirement"] == "optional"


def test_runner_policy_owner_can_decide_member_cannot(
    db_session: Session, test_user: User
):
    """Owner and single-user accounts may decline. Other members may not."""
    account = crud_account.get(db_session, id=test_user.account_id)
    assert account is not None
    crud_account.update(
        db_session,
        db_obj=account,
        obj_in={"primary_user_id": test_user.id},
    )
    member = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "member@example.com",
            "username": "memberuser",
            "full_name": "Member User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )

    owner_policy = account_hooks.oss_default_runner_policy(db_session, test_user)
    member_policy = account_hooks.oss_default_runner_policy(db_session, member)

    assert owner_policy["can_decide"] is True
    assert owner_policy["requirement"] == "optional"
    assert member_policy["can_decide"] is False
    assert member_policy["requirement"] == "optional"
