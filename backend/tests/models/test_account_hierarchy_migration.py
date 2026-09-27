"""Account hierarchy revisions (#986) on a seeded database.

Seeds two accounts, a user in each holding the same verified address, a user
holding an unverified duplicate of it, teams, roles and a budget. Then walks
the six revisions down and up again (twice up, for idempotency) inside the
test transaction and checks the backfill.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models import models
from preloop.models.models.person import normalize_email

VERSIONS = (
    Path(__file__).resolve().parents[2] / "preloop" / "models" / "alembic" / "versions"
)
REVISIONS = (
    "20260928_account_hierarchy",
    "20260928_access_grants",
    "20260928_person_membership",
    "20260928_person_backfill",
    "20260928_person_constraints",
    "20260928_share_tag_rule",
)
NEW_TABLES = {
    "person",
    "account_access_grant",
    "account_access_grant_target",
    "resource_share",
    "resource_share_recipient",
    "resource_tag",
    "tag_key_policy",
    "access_rule",
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _downgrade_all(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for name in reversed(REVISIONS):
            _load(name).downgrade()


def _upgrade_all(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for name in REVISIONS:
            _load(name).upgrade()


def _seed(db_session) -> dict[str, uuid.UUID]:
    now = datetime.now(timezone.utc)
    acme = models.Account(organization_name="acme")
    globex = models.Account(organization_name="globex")
    db_session.add_all([acme, globex])
    db_session.flush()

    def user(account, name, email, verified, last_login):
        row = models.User(
            account_id=account.id,
            username=f"{name}-{uuid.uuid4().hex[:8]}",
            email=email,
            email_verified=verified,
            hashed_password=f"hash-{name}",
            user_source="local",
            last_login=last_login,
        )
        db_session.add(row)
        return row

    older = user(acme, "alice-acme", "alice@example.com", True, now - timedelta(days=3))
    newer = user(globex, "alice-globex", " Alice@Example.COM", True, now)
    squatter = user(globex, "alice-squat", "alice@example.com", False, None)
    solo = user(acme, "bob", "bob@example.com", False, None)
    # Imported rows (CSV, LDAP, tracker sync) can carry tabs and newlines.
    tabbed = user(acme, "carol", "\tCarol@Example.com\n", True, now)
    db_session.flush()

    team = models.Team(account_id=acme.id, name="platform")
    db_session.add(team)
    db_session.flush()
    db_session.add(models.TeamMembership(team_id=team.id, user_id=older.id))
    role = models.Role(name=f"seed-role-{uuid.uuid4().hex[:8]}", account_id=acme.id)
    db_session.add(role)
    db_session.flush()
    db_session.add(models.UserRole(user_id=older.id, role_id=role.id))
    db_session.add(
        models.BudgetPolicy(
            account_id=acme.id,
            subject_type="account",
            period=models.BudgetPeriod.monthly,
            hard_limit_usd=100.0,
        )
    )
    db_session.flush()
    return {
        "acme": acme.id,
        "globex": globex.id,
        "older": older.id,
        "newer": newer.id,
        "squatter": squatter.id,
        "solo": solo.id,
        "tabbed": tabbed.id,
    }


def _users(connection, ids):
    rows = connection.execute(
        text(
            "SELECT id, person_id, membership_kind, access_grant_id, hashed_password"
            ' FROM "user" WHERE id = ANY(:ids)'
        ),
        {"ids": list(ids)},
    ).mappings()
    return {row["id"]: dict(row) for row in rows}


def _assert_backfilled(connection, seeded) -> None:
    accounts = connection.execute(
        text(
            "SELECT id, parent_account_id, root_account_id, hierarchy_path,"
            " hierarchy_depth FROM account"
        )
    ).mappings()
    for account in accounts:
        assert account["parent_account_id"] is None
        assert account["root_account_id"] == account["id"]
        assert account["hierarchy_path"] == [account["id"]]
        assert account["hierarchy_depth"] == 0

    unlinked = connection.execute(
        text(
            'SELECT count(*) FROM "user"'
            " WHERE person_id IS NULL OR membership_kind <> 'direct'"
            " OR access_grant_id IS NOT NULL"
        )
    ).scalar()
    assert unlinked == 0

    users = _users(
        connection, [seeded[k] for k in ("older", "newer", "squatter", "solo")]
    )
    shared = users[seeded["older"]]["person_id"]
    assert users[seeded["newer"]]["person_id"] == shared
    assert users[seeded["squatter"]]["person_id"] != shared
    assert users[seeded["solo"]]["person_id"] not in {
        shared,
        users[seeded["squatter"]]["person_id"],
    }
    for key in ("older", "newer", "squatter", "solo"):
        assert users[seeded[key]]["hashed_password"] == f"hash-{key_name(key)}"

    person = (
        connection.execute(
            text(
                "SELECT email_normalized, email_verified_at, primary_user_id,"
                " last_active_user_id FROM person WHERE id = :id"
            ),
            {"id": shared},
        )
        .mappings()
        .one()
    )
    assert person["email_normalized"] == "alice@example.com"
    assert person["email_verified_at"] is not None
    assert person["primary_user_id"] == seeded["newer"]
    assert person["last_active_user_id"] == seeded["newer"]

    provisional = (
        connection.execute(
            text(
                "SELECT email_verified_at, primary_user_id FROM person WHERE id = :id"
            ),
            {"id": users[seeded["squatter"]]["person_id"]},
        )
        .mappings()
        .one()
    )
    assert provisional["email_verified_at"] is None
    assert provisional["primary_user_id"] == seeded["squatter"]

    tabbed = connection.execute(
        text(
            "SELECT p.email_normalized FROM person p"
            ' JOIN "user" u ON u.person_id = p.id WHERE u.id = :id'
        ),
        {"id": seeded["tabbed"]},
    ).scalar()
    assert tabbed == "carol@example.com" == normalize_email("\tCarol@Example.com\n")

    orphans = connection.execute(
        text(
            "SELECT count(*) FROM person p WHERE NOT EXISTS"
            ' (SELECT 1 FROM "user" u WHERE u.person_id = p.id)'
        )
    ).scalar()
    assert orphans == 0


def key_name(key: str) -> str:
    return {
        "older": "alice-acme",
        "newer": "alice-globex",
        "squatter": "alice-squat",
        "solo": "bob",
        "tabbed": "carol",
    }[key]


def test_downgrade_then_upgrade_backfills_seeded_rows(db_session):
    seeded = _seed(db_session)
    connection = db_session.connection()
    before = _users(
        connection, [seeded[k] for k in seeded if k not in ("acme", "globex")]
    )

    _downgrade_all(connection)
    inspector = inspect(connection)
    assert not NEW_TABLES & set(inspector.get_table_names())
    assert "person_id" not in {c["name"] for c in inspector.get_columns("user")}
    assert "hierarchy_path" not in {c["name"] for c in inspector.get_columns("account")}
    assert "access_grant_id" not in {
        c["name"] for c in inspector.get_columns("user_role")
    }
    # Teams, roles and budgets survive the downgrade untouched.
    assert (
        connection.execute(
            text("SELECT count(*) FROM user_role WHERE user_id = :id"),
            {"id": seeded["older"]},
        ).scalar()
        == 1
    )
    assert (
        connection.execute(
            text("SELECT count(*) FROM budget_policies WHERE account_id = :id"),
            {"id": seeded["acme"]},
        ).scalar()
        == 1
    )

    _upgrade_all(connection)
    _assert_backfilled(connection, seeded)

    after = _users(connection, before)
    assert {k: v["hashed_password"] for k, v in after.items()} == {
        k: v["hashed_password"] for k, v in before.items()
    }


def test_upgrade_is_idempotent(db_session):
    seeded = _seed(db_session)
    connection = db_session.connection()
    _downgrade_all(connection)
    _upgrade_all(connection)
    persons = connection.execute(
        text('SELECT id, person_id FROM "user" ORDER BY id')
    ).all()
    person_count = connection.execute(text("SELECT count(*) FROM person")).scalar()

    _upgrade_all(connection)

    assert (
        connection.execute(text('SELECT id, person_id FROM "user" ORDER BY id')).all()
        == persons
    )
    assert connection.execute(text("SELECT count(*) FROM person")).scalar() == (
        person_count
    )
    _assert_backfilled(connection, seeded)


def test_verified_duplicates_in_one_account_keep_separate_persons(db_session):
    """UNIQUE (person_id, account_id): only one row per account joins a person."""
    now = datetime.now(timezone.utc)
    account = models.Account(organization_name="twins")
    db_session.add(account)
    db_session.flush()
    rows = []
    for offset in (1, 2):
        row = models.User(
            account_id=account.id,
            username=f"twin-{uuid.uuid4().hex[:8]}",
            email="twin@example.com",
            email_verified=True,
            user_source="local",
            last_login=now - timedelta(days=offset),
        )
        db_session.add(row)
        rows.append(row)
    db_session.flush()
    connection = db_session.connection()

    _downgrade_all(connection)
    _upgrade_all(connection)

    users = _users(connection, [r.id for r in rows])
    recent, stale = users[rows[0].id], users[rows[1].id]
    assert recent["person_id"] != stale["person_id"]
    verified = connection.execute(
        text("SELECT id, email_verified_at FROM person WHERE id = ANY(:ids)"),
        {"ids": [recent["person_id"], stale["person_id"]]},
    ).all()
    verified_at = dict(verified)
    assert verified_at[recent["person_id"]] is not None
    assert verified_at[stale["person_id"]] is None


def _upgrade(connection, names) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for name in names:
            _load(name).upgrade()


def _person_id_nullable(connection) -> bool:
    columns = inspect(connection).get_columns("user")
    return next(c["nullable"] for c in columns if c["name"] == "person_id")


def test_rows_written_between_backfill_and_constraints_are_linked(db_session):
    """The backfill commits without NOT NULL; old pods may still insert rows.

    The constraints revision links those stragglers to provisional persons
    before it sets NOT NULL, so the upgrade does not fail on them.
    """
    seeded = _seed(db_session)
    connection = db_session.connection()
    _downgrade_all(connection)
    split = REVISIONS.index("20260928_person_backfill") + 1
    _upgrade(connection, REVISIONS[:split])

    assert _person_id_nullable(connection)
    straggler = uuid.uuid4()
    connection.execute(
        text(
            'INSERT INTO "user" (id, account_id, username, email, email_verified,'
            " user_source, is_active, created_at, updated_at)"
            " VALUES (:id, :account, :username, 'alice@example.com', TRUE,"
            " 'local', TRUE, now(), now())"
        ),
        {
            "id": straggler,
            "account": seeded["acme"],
            "username": f"late-{straggler.hex[:8]}",
        },
    )

    _upgrade(connection, REVISIONS[split:])

    assert not _person_id_nullable(connection)
    person = (
        connection.execute(
            text(
                "SELECT p.id, p.email_verified_at, p.primary_user_id FROM person p"
                ' JOIN "user" u ON u.person_id = p.id WHERE u.id = :id'
            ),
            {"id": straggler},
        )
        .mappings()
        .one()
    )
    # Provisional, never merged into the verified alice person.
    assert person["email_verified_at"] is None
    assert person["primary_user_id"] == straggler
    shared = _users(connection, [seeded["older"]])[seeded["older"]]["person_id"]
    assert person["id"] != shared
