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
    CrossAccountReferenceError,
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
        db,
        account_id=account_id,
        connection_id=connection.id,
        idp_group="engineers",
        role="member",
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
                db_session,
                account_id=test_user.account_id,
                connection_id=connection.id,
                idp_group="g",
                role="owner",
            )
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            crud_sso_group_mapping.create_mapping(
                db_session,
                account_id=test_user.account_id,
                connection_id=connection.id,
                idp_group="g",
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


def test_verification_race_on_the_unique_index_keeps_the_session_usable(
    db_session, test_user, monkeypatch
):
    """Another account verifies between the pre-check and the write."""
    other = _other_account(db_session)
    theirs = crud_account_verified_domain.create_claim(
        db_session, account_id=other.id, domain="race.example", challenge_token="a"
    )
    mine = crud_account_verified_domain.create_claim(
        db_session,
        account_id=test_user.account_id,
        domain="race.example",
        challenge_token="b",
    )
    theirs.verified_at = utc_now()
    db_session.flush()
    # Simulate the pre-check running before the other account committed.
    monkeypatch.setattr(
        crud_account_verified_domain, "get_verified", lambda db, domain: None
    )
    with pytest.raises(DomainVerifiedElsewhereError):
        crud_account_verified_domain.record_check(db_session, claim=mine, verified=True)
    assert mine.verified_at is None
    # The caller's transaction survived: the attempt is recorded and usable.
    db_session.refresh(mine)
    assert mine.verified_at is None
    assert mine.last_checked_at is not None


def test_group_mappings_stay_inside_the_account(db_session, test_user):
    from preloop.models.crud import crud_team

    other = _other_account(db_session)
    mine = _connection(db_session, test_user.account_id)
    theirs = _connection(db_session, other.id)
    their_team = crud_team.create(
        db_session, obj_in={"account_id": other.id, "name": "Theirs"}
    )
    with pytest.raises(CrossAccountReferenceError):
        crud_sso_group_mapping.create_mapping(
            db_session,
            account_id=test_user.account_id,
            connection_id=mine.id,
            idp_group="eng",
            team_id=their_team.id,
        )
    with pytest.raises(CrossAccountReferenceError):
        crud_sso_group_mapping.create_mapping(
            db_session,
            account_id=test_user.account_id,
            connection_id=theirs.id,
            idp_group="eng",
            role="viewer",
        )
    mapping = crud_sso_group_mapping.create_mapping(
        db_session,
        account_id=other.id,
        connection_id=theirs.id,
        idp_group="eng",
        role="viewer",
    )
    assert (
        crud_sso_group_mapping.list_for_connection(
            db_session, connection_id=theirs.id, account_id=test_user.account_id
        )
        == []
    )
    assert (
        crud_sso_group_mapping.get_in_connection(
            db_session,
            mapping_id=mapping.id,
            connection_id=theirs.id,
            account_id=test_user.account_id,
        )
        is None
    )


def test_connection_secret_refs_must_belong_to_the_account(db_session, test_user):
    from preloop.services.secret_service import get_secret_service

    other = _other_account(db_session)
    their_secret = get_secret_service().create_local_secret_reference(
        db_session,
        account_id=other.id,
        name="theirs",
        secret_kind="sso_oidc_client_secret",
        secret_value="unused-value",
    )
    with pytest.raises(CrossAccountReferenceError):
        crud_account_sso_connection.create_for_account(
            db_session,
            account_id=test_user.account_id,
            protocol="oidc",
            fields={"name": "x", "oidc_client_secret_ref_id": their_secret.id},
        )
    connection = _connection(db_session, test_user.account_id)
    with pytest.raises(CrossAccountReferenceError):
        crud_account_sso_connection.apply_update(
            db_session,
            connection=connection,
            fields={"saml_sp_signing_key_ref_id": their_secret.id},
        )


def test_inherited_get_and_delete_cannot_cross_accounts(db_session, test_user):
    other = _other_account(db_session)
    theirs = _connection(db_session, other.id)
    mapping = crud_sso_group_mapping.create_mapping(
        db_session,
        account_id=other.id,
        connection_id=theirs.id,
        idp_group="eng",
        role="viewer",
    )
    mine = test_user.account_id
    assert crud_sso_group_mapping.get(db_session, mapping.id, account_id=mine) is None
    assert crud_sso_group_mapping.get(db_session, mapping.id, account_id=other.id)
    assert crud_sso_group_mapping.get_multi(db_session, account_id=mine) == []
    crud_sso_replay_guard.claim(
        db_session,
        connection_id=theirs.id,
        guard_id="theirs",
        expires_at=utc_now() + timedelta(minutes=5),
    )
    guard = db_session.query(models.SsoReplayGuard).filter_by(guard_id="theirs").one()
    for crud, row_id in (
        (crud_sso_replay_guard, guard.id),
        (crud_sso_group_mapping, mapping.id),
        (crud_account_sso_connection, theirs.id),
    ):
        with pytest.raises(TypeError):
            crud.delete(db_session, id=row_id)
        assert crud.delete(db_session, id=row_id, account_id=mine) is None
    assert crud_account_sso_connection.get(db_session, theirs.id) is not None
    assert (
        crud_sso_group_mapping.delete(db_session, id=mapping.id, account_id=other.id)
        is not None
    )


def test_policy_upsert_survives_a_concurrent_first_write(db_session, test_user):
    """The insert that loses the race updates instead of failing."""
    account_id = test_user.account_id
    db_session.execute(
        models.AccountSsoPolicy.__table__.insert().values(
            id=uuid.uuid4(), account_id=account_id, enforce=False
        )
    )
    # Simulates a writer whose existence check ran before that insert.
    row = crud_account_sso_policy.upsert(
        db_session, account_id=account_id, fields={"enforce": True}
    )
    assert row.enforce is True
    rows = db_session.query(models.AccountSsoPolicy).filter_by(account_id=account_id)
    assert rows.count() == 1


@pytest.mark.parametrize(
    "crud",
    [
        crud_account_sso_connection,
        crud_account_sso_policy,
        crud_account_verified_domain,
        crud_sso_group_mapping,
        crud_scim_token,
        crud_sso_replay_guard,
    ],
)
@pytest.mark.parametrize(
    "field",
    [
        "team_id",
        "connection_id",
        "oidc_client_secret_ref_id",
        "saml_sp_signing_key_ref_id",
    ],
)
def test_generic_writes_require_validated_helpers(crud, field: str) -> None:
    """No generic write may bypass the named helpers' account checks."""
    fields = {field: uuid.uuid4()}
    with pytest.raises(TypeError, match="requires a named write helper"):
        crud.create(None, obj_in=fields)
    with pytest.raises(TypeError, match="requires a named write helper"):
        crud.update(None, db_obj=None, obj_in=fields)
