"""Instance-level check for whether the sign-in page offers single sign-on."""

from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_account_verified_domain,
)


def _connection(db, account_id, *, enabled: bool):
    row = crud_account_sso_connection.create_for_account(
        db,
        account_id=account_id,
        protocol="oidc",
        fields={
            "name": "Example IdP",
            "oidc_issuer": "https://idp.example.com",
            "oidc_client_id": "client-1",
        },
    )
    row.enabled = enabled
    db.flush()
    return row


def _domain(db, account_id, *, verified: bool):
    claim = crud_account_verified_domain.create_claim(
        db, account_id=account_id, domain="example.com", challenge_token="t"
    )
    if verified:
        crud_account_verified_domain.record_check(db, claim=claim, verified=True)
    db.flush()
    return claim


def test_no_connections_means_not_configured(db_session):
    assert crud_account_sso_connection.login_configured(db_session) is False


def test_enabled_connection_with_verified_domain(db_session, test_user):
    _connection(db_session, test_user.account_id, enabled=True)
    _domain(db_session, test_user.account_id, verified=True)
    assert crud_account_sso_connection.login_configured(db_session) is True


def test_disabled_connection_is_not_configured(db_session, test_user):
    _connection(db_session, test_user.account_id, enabled=False)
    _domain(db_session, test_user.account_id, verified=True)
    assert crud_account_sso_connection.login_configured(db_session) is False


def test_unverified_domain_is_not_configured(db_session, test_user):
    _connection(db_session, test_user.account_id, enabled=True)
    _domain(db_session, test_user.account_id, verified=False)
    assert crud_account_sso_connection.login_configured(db_session) is False


def test_domain_in_another_account_does_not_count(db_session, test_user):
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    _connection(db_session, test_user.account_id, enabled=True)
    _domain(db_session, other.id, verified=True)
    assert crud_account_sso_connection.login_configured(db_session) is False
