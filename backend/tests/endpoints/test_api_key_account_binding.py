"""An API key authenticates only inside the account it was minted for (#984)."""

import logging

import pytest
from fastapi import HTTPException

from preloop.api.auth import jwt as jwt_module
from preloop.models.crud import crud_account, crud_api_key


def _other_account(db_session):
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )


def test_key_bound_to_another_account_is_rejected_like_an_invalid_key(
    db_session, test_user, caplog
):
    """A key whose account differs from its user's account fails closed."""
    other = _other_account(db_session)
    api_key, _token = crud_api_key.create_runtime_key(
        db_session,
        name="Cross-account key",
        account_id=other.id,
        user_id=test_user.id,
    )

    with caplog.at_level(logging.WARNING, logger=jwt_module.logger.name):
        with pytest.raises(HTTPException) as excinfo:
            jwt_module._authenticate_with_api_key(db_session, api_key)

    assert excinfo.value.status_code == 401
    # Same client response as an unknown key, and no account ids leak.
    assert excinfo.value.detail == "Invalid API key"
    assert str(other.id) not in str(excinfo.value.detail)
    assert str(test_user.account_id) not in str(excinfo.value.detail)
    assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}
    # A rejected key is never marked as used.
    assert api_key.last_used_at is None
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "api_key_account_mismatch"
    ]
    assert len(warnings) == 1
    assert warnings[0].api_key_id == str(api_key.id)


def test_token_lookup_returns_no_user_for_a_cross_account_key(db_session, test_user):
    """The bearer path used by the gateway resolves no user for such a key."""
    other = _other_account(db_session)
    _api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Cross-account key",
        account_id=other.id,
        user_id=test_user.id,
    )

    assert jwt_module.get_user_from_token_if_valid_sync(token, db_session) is None


def test_key_in_the_users_account_still_authenticates(db_session, test_user):
    """The binding check does not reject a key minted in the user's account."""
    api_key, _token = crud_api_key.create_runtime_key(
        db_session,
        name="Same-account key",
        account_id=test_user.account_id,
        user_id=test_user.id,
    )

    user = jwt_module._authenticate_with_api_key(db_session, api_key)

    assert user.id == test_user.id
    assert api_key.last_used_at is not None
