"""Real-DB tests for the identity fields on GET /api/v1/auth/users/me.

Mobile and web surfaces answer "is this pending approval waiting for me?" by
intersecting the caller's identity with an approval workflow's
``approver_user_ids`` / ``approver_team_ids``. That needs the profile endpoint
to carry the caller's user id, account id and team ids.
"""

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from preloop.api.auth import router as auth_router
from preloop.models.crud import crud_account
from preloop.models.models.team import Team, TeamMembership
from preloop.models.models.user import User

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
