"""Tests for the user impersonation endpoints.

EE/plugin gating note
---------------------
The impersonation router (``preloop.api.endpoints.impersonation``) is NOT
registered in the open-source FastAPI app. ``preloop/api/app.py`` documents
that impersonation is "now loaded exclusively via the plugin system". As a
result there is no HTTP route to exercise through the ``client`` fixture in
this OSS build, so the route-registration test below asserts the absence of
the route (documenting the gating) and the behavioural tests call the
endpoint coroutines directly with a ``db_session`` and constructed users --
the same direct-call strategy used by ``tests/endpoints/test_projects.py``.

These tests still provide real coverage of the endpoint logic: superuser
gating, target lookup/validation, token claims, and audit logging.
"""

import asyncio

import pytest
from fastapi import HTTPException

from preloop.api.auth.jwt import decode_token
from preloop.api.endpoints import impersonation
from preloop.models.crud import crud_account, crud_audit_log, crud_user
from tests.route_paths import collect_route_paths


def _run(coro):
    """Execute an async endpoint coroutine to completion."""
    return asyncio.run(coro)


def _make_account(db, name="Imp Org"):
    return crud_account.create(
        db, obj_in={"organization_name": name, "is_active": True}
    )


def _make_user(db, account, email, *, is_superuser=False, is_active=True):
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Imp User",
            "is_active": is_active,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
            "is_superuser": is_superuser,
        },
    )
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Route-registration / gating
# ---------------------------------------------------------------------------


def test_impersonation_routes_not_registered_in_oss_app(app):
    """Impersonation is plugin-gated and must not be mounted in the OSS app."""
    paths = collect_route_paths(app.routes)
    assert not any("/admin/impersonate" in path for path in paths)


def test_impersonation_module_exposes_router_and_handlers():
    """The module is importable and exposes its router + handler coroutines."""
    assert impersonation.router.prefix == "/admin/impersonate"
    assert asyncio.iscoroutinefunction(impersonation.impersonate_user)
    assert asyncio.iscoroutinefunction(impersonation.stop_impersonation)


# ---------------------------------------------------------------------------
# impersonate_user
# ---------------------------------------------------------------------------


def test_impersonate_user_non_superuser_forbidden(db_session):
    """A non-superuser caller must be rejected with 403."""
    account = _make_account(db_session)
    caller = _make_user(db_session, account, "caller@example.com", is_superuser=False)
    target = _make_user(db_session, account, "target@example.com")

    with pytest.raises(HTTPException) as exc:
        _run(
            impersonation.impersonate_user(
                user_id=str(target.id), db=db_session, current_user=caller
            )
        )
    assert exc.value.status_code == 403


def test_impersonate_user_missing_target_returns_404(db_session):
    """Impersonating a non-existent user id should raise 404."""
    account = _make_account(db_session)
    superuser = _make_user(db_session, account, "su@example.com", is_superuser=True)

    import uuid

    with pytest.raises(HTTPException) as exc:
        _run(
            impersonation.impersonate_user(
                user_id=str(uuid.uuid4()), db=db_session, current_user=superuser
            )
        )
    assert exc.value.status_code == 404


def test_impersonate_user_inactive_target_returns_400(db_session):
    """Impersonating an inactive user should raise 400."""
    account = _make_account(db_session)
    superuser = _make_user(db_session, account, "su2@example.com", is_superuser=True)
    target = _make_user(db_session, account, "inactive@example.com", is_active=False)

    with pytest.raises(HTTPException) as exc:
        _run(
            impersonation.impersonate_user(
                user_id=str(target.id), db=db_session, current_user=superuser
            )
        )
    assert exc.value.status_code == 400


def test_impersonate_user_success_returns_token_and_metadata(db_session):
    """A superuser should receive a bearer token plus target/impersonator info."""
    account = _make_account(db_session)
    superuser = _make_user(db_session, account, "su3@example.com", is_superuser=True)
    target = _make_user(db_session, account, "target3@example.com")

    result = _run(
        impersonation.impersonate_user(
            user_id=str(target.id), db=db_session, current_user=superuser
        )
    )

    assert result["token_type"] == "bearer"
    assert result["expires_in_hours"] == 8
    assert result["user"]["id"] == str(target.id)
    assert result["user"]["email"] == target.email
    assert result["user"]["account_id"] == str(target.account_id)
    assert result["impersonated_by"]["id"] == str(superuser.id)
    assert isinstance(result["access_token"], str) and result["access_token"]


def test_impersonate_user_token_encodes_impersonation_claims(db_session):
    """The issued JWT should carry sub=target and impersonated_by=caller."""
    account = _make_account(db_session)
    superuser = _make_user(db_session, account, "su4@example.com", is_superuser=True)
    target = _make_user(db_session, account, "target4@example.com")

    result = _run(
        impersonation.impersonate_user(
            user_id=str(target.id), db=db_session, current_user=superuser
        )
    )

    from preloop.api.auth.jwt import SECRET_KEY, ALGORITHM
    import jwt as pyjwt

    payload = pyjwt.decode(result["access_token"], SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["sub"] == str(target.id)
    assert payload["impersonated_by"] == str(superuser.id)
    assert payload["account_id"] == str(target.account_id)
    assert "impersonation_started_at" in payload

    # The token also decodes through the app's own helper with sub=target.
    token_data = decode_token(result["access_token"])
    assert token_data.sub == str(target.id)


def test_impersonate_user_writes_audit_log(db_session):
    """A successful impersonation should record a user.impersonate audit entry."""
    account = _make_account(db_session)
    superuser = _make_user(db_session, account, "su5@example.com", is_superuser=True)
    target = _make_user(db_session, account, "target5@example.com")

    _run(
        impersonation.impersonate_user(
            user_id=str(target.id), db=db_session, current_user=superuser
        )
    )

    logs = crud_audit_log.get_by_account(
        db_session, account_id=account.id, action="user.impersonate"
    )
    assert len(logs) == 1
    entry = logs[0]
    assert entry.status == "success"
    assert entry.resource_id == str(target.id)
    assert entry.details["impersonated_user_id"] == str(target.id)
    assert entry.details["impersonator_user_id"] == str(superuser.id)


def test_impersonate_user_allows_cross_account_target(db_session):
    """Superusers are platform-wide: impersonation is NOT account-scoped.

    This documents intended behaviour -- a superuser can impersonate a user in
    a different account for support/debugging. The issued token carries the
    *target's* account_id, not the caller's.
    """
    caller_account = _make_account(db_session, name="Caller Org")
    target_account = _make_account(db_session, name="Target Org")
    superuser = _make_user(
        db_session, caller_account, "su6@example.com", is_superuser=True
    )
    target = _make_user(db_session, target_account, "foreign@example.com")

    result = _run(
        impersonation.impersonate_user(
            user_id=str(target.id), db=db_session, current_user=superuser
        )
    )

    assert result["user"]["account_id"] == str(target_account.id)
    assert result["user"]["account_id"] != str(caller_account.id)


# ---------------------------------------------------------------------------
# stop_impersonation
# ---------------------------------------------------------------------------


def test_stop_impersonation_returns_message_and_user(db_session):
    """Stopping impersonation returns a confirmation message and the user id."""
    account = _make_account(db_session)
    user = _make_user(db_session, account, "stop@example.com")

    result = _run(impersonation.stop_impersonation(db=db_session, current_user=user))

    assert "Impersonation stopped" in result["message"]
    assert result["user_id"] == str(user.id)


def test_stop_impersonation_writes_audit_log(db_session):
    """Stopping impersonation should record a user.impersonate.stop audit entry."""
    account = _make_account(db_session)
    user = _make_user(db_session, account, "stop2@example.com")

    _run(impersonation.stop_impersonation(db=db_session, current_user=user))

    logs = crud_audit_log.get_by_account(
        db_session, account_id=account.id, action="user.impersonate.stop"
    )
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].details["impersonated_user_id"] == str(user.id)
