"""Enterprise SSO tables (EE #252): migration round trip and CRUD invariants."""

import importlib.util
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_account_sso_policy,
    crud_account_verified_domain,
    crud_scim_token,
    crud_sso_group_mapping,
    crud_sso_replay_guard,
)
from preloop.models.crud.enterprise_sso import (
    DomainVerifiedElsewhereError,
    hash_scim_token,
    utc_now,
)
from preloop.models.models.base import Base

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261112_enterprise_sso.py"
)
TABLES = (
    "account_sso_connection",
    "account_verified_domain",
    "account_sso_policy",
    "sso_group_mapping",
    "scim_token",
    "sso_replay_guard",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("enterprise_sso_mig", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _other_account(db):
    return crud_account.create(
        db, obj_in={"organization_name": "Other Org", "is_active": True}
    )


def _connection(db, account_id, name="Example IdP"):
    return crud_account_sso_connection.create_for_account(
        db,
        account_id=account_id,
        protocol="oidc",
        fields={
            "name": name,
            "oidc_issuer": "https://idp.example.com",
            "oidc_client_id": "client-1",
        },
    )


def _seed(db, account_id):
    connection = _connection(db, account_id)
    crud_account_verified_domain.create_claim(
        db, account_id=account_id, domain="example.com", challenge_token="t"
    )
    crud_account_sso_policy.upsert(db, account_id=account_id, fields={"enforce": True})
    crud_sso_group_mapping.create_mapping(
        db, connection_id=connection.id, idp_group="engineers", role="member"
    )
    crud_scim_token.create_token(
        db, account_id=account_id, name="scim", token="scim_secret", prefix="scim_se"
    )
    crud_sso_replay_guard.claim(
        db,
        connection_id=connection.id,
        guard_id="nonce-1",
        expires_at=utc_now() + timedelta(minutes=10),
    )
    db.flush()


def _round_trip(db):
    migration = _load_migration()
    connection = db.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        names = set(inspect(connection).get_table_names())
        assert not names & set(TABLES)
        migration.upgrade()
    names = set(inspect(connection).get_table_names())
    assert set(TABLES) <= names


def test_migration_round_trip_on_empty_tables(db_session):
    _round_trip(db_session)


def test_migration_round_trip_on_seeded_database(db_session, test_user):
    _seed(db_session, test_user.account_id)
    _round_trip(db_session)
    # Upgrading again leaves usable, empty tables.
    assert (
        crud_account_sso_connection.list_for_account(
            db_session, account_id=test_user.account_id
        )
        == []
    )
    _seed(db_session, test_user.account_id)


def test_model_metadata_matches_the_migrated_schema(db_session):
    diffs = compare_metadata(
        MigrationContext.configure(db_session.connection()), Base.metadata
    )

    def touches(diff) -> bool:
        for item in diff if isinstance(diff, list) else [diff]:
            if not isinstance(item, tuple):
                continue
            for part in item[1:]:
                table = getattr(part, "table", None)
                if getattr(part, "name", None) in TABLES:
                    return True
                if getattr(table, "name", None) in TABLES:
                    return True
                if isinstance(part, str) and part in TABLES:
                    return True
        return False

    assert [diff for diff in diffs if touches(diff)] == []


def test_connection_reads_are_account_scoped(db_session, test_user):
    other = _other_account(db_session)
    mine = _connection(db_session, test_user.account_id)
    theirs = _connection(db_session, other.id)
    assert (
        crud_account_sso_connection.get_in_account(
            db_session, connection_id=theirs.id, account_id=test_user.account_id
        )
        is None
    )
    listed = crud_account_sso_connection.list_for_account(
        db_session, account_id=test_user.account_id
    )
    assert [row.id for row in listed] == [mine.id]


def test_owner_can_never_be_the_default_or_mapped_role(db_session, test_user):
    connection = _connection(db_session, test_user.account_id)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            crud_account_sso_connection.apply_update(
                db_session, connection=connection, fields={"default_role": "owner"}
            )
    db_session.expire_all()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            crud_sso_group_mapping.create_mapping(
                db_session, connection_id=connection.id, idp_group="g", role="owner"
            )
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            crud_sso_group_mapping.create_mapping(
                db_session, connection_id=connection.id, idp_group="g"
            )


def test_domain_verified_by_one_account_only(db_session, test_user):
    other = _other_account(db_session)
    mine = crud_account_verified_domain.create_claim(
        db_session,
        account_id=test_user.account_id,
        domain="Example.COM",
        challenge_token="a",
    )
    theirs = crud_account_verified_domain.create_claim(
        db_session, account_id=other.id, domain="example.com", challenge_token="b"
    )
    assert mine.domain == "example.com"
    crud_account_verified_domain.record_check(db_session, claim=mine, verified=True)
    assert mine.verified_at is not None
    with pytest.raises(DomainVerifiedElsewhereError):
        crud_account_verified_domain.record_check(
            db_session, claim=theirs, verified=True
        )
    assert theirs.verified_at is None
    assert theirs.last_checked_at is not None
    held = crud_account_verified_domain.get_verified(db_session, domain="EXAMPLE.com")
    assert held.account_id == test_user.account_id


def test_unique_index_blocks_a_second_verified_row(db_session, test_user):
    """The database refuses it even when the CRUD check is bypassed."""
    other = _other_account(db_session)
    for account_id in (test_user.account_id, other.id):
        crud_account_verified_domain.create_claim(
            db_session, account_id=account_id, domain="example.org", challenge_token="x"
        )
    table = models.AccountVerifiedDomain.__table__
    verify = table.update().values(verified_at=utc_now())
    db_session.execute(verify.where(table.c.account_id == test_user.account_id))
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(verify.where(table.c.account_id == other.id))


def test_failed_check_records_attempt_only(db_session, test_user):
    claim = crud_account_verified_domain.create_claim(
        db_session,
        account_id=test_user.account_id,
        domain="example.net",
        challenge_token="x",
    )
    crud_account_verified_domain.record_check(db_session, claim=claim, verified=False)
    assert claim.verified_at is None
    assert claim.last_checked_at is not None


def test_policy_upsert_keeps_one_row(db_session, test_user):
    account_id = test_user.account_id
    assert (
        crud_account_sso_policy.get_for_account(db_session, account_id=account_id)
        is None
    )
    first = crud_account_sso_policy.upsert(
        db_session, account_id=account_id, fields={"enforce": True}
    )
    second = crud_account_sso_policy.upsert(
        db_session, account_id=account_id, fields={"session_max_age_seconds": 3600}
    )
    assert first.id == second.id
    assert second.enforce is True
    assert second.session_max_age_seconds == 3600


def test_scim_token_stores_only_the_hash(db_session, test_user):
    token = "scim_" + uuid.uuid4().hex
    row = crud_scim_token.create_token(
        db_session,
        account_id=test_user.account_id,
        name="okta",
        token=token,
        prefix=token[:8],
    )
    assert row.token_hash == hash_scim_token(token)
    assert token not in str(row.to_dict().values())
    assert crud_scim_token.get_active_by_token(db_session, token=token).id == row.id
    crud_scim_token.revoke(db_session, token=row)
    assert crud_scim_token.get_active_by_token(db_session, token=token) is None
    other = _other_account(db_session)
    assert (
        crud_scim_token.get_in_account(db_session, token_id=row.id, account_id=other.id)
        is None
    )


def test_replay_guard_accepts_an_id_once_and_purges_expired(db_session, test_user):
    connection = _connection(db_session, test_user.account_id)
    other = _connection(db_session, test_user.account_id, name="Second IdP")
    now = utc_now()
    later = now + timedelta(minutes=10)
    claim = crud_sso_replay_guard.claim
    assert claim(
        db_session, connection_id=connection.id, guard_id="n", expires_at=later
    )
    assert not claim(
        db_session, connection_id=connection.id, guard_id="n", expires_at=later
    )
    assert claim(db_session, connection_id=other.id, guard_id="n", expires_at=later)
    assert claim(
        db_session,
        connection_id=connection.id,
        guard_id="old",
        expires_at=now - timedelta(seconds=1),
    )
    assert crud_sso_replay_guard.purge_expired(db_session, now=now) == 1
    assert not claim(
        db_session, connection_id=connection.id, guard_id="n", expires_at=later
    )
