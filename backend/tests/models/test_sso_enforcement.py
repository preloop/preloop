"""Verified domain matching uses the same IDNA form for selective revocation."""

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_person, sso_enforcement


@pytest.mark.parametrize(
    "email",
    ["member@bücher.example", "member@XN--BCHER-KVA.EXAMPLE", "member@bücher.example."],
)
def test_selective_revocation_matches_unicode_domain(
    db_session: Session, test_user: models.User, email: str
) -> None:
    crud_person.update_profile(
        db_session, membership_id=test_user.id, changes={"email": email}
    )
    generation = test_user.auth_generation
    assert (
        sso_enforcement.revoke_non_sso(
            db_session,
            account_id=test_user.account_id,
            domains={"xn--bcher-kva.example"},
        )
        == 1
    )
    assert test_user.non_sso_auth_generation == 1
    assert test_user.auth_generation == generation


def test_only_global_system_owner_receives_recovery_notices(db_session, test_user):
    from preloop.models.crud import crud_user

    custom = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": "custom-owner",
            "email": "custom-owner@example.test",
            "is_active": True,
        },
    )
    role = models.Role(
        account_id=test_user.account_id, name="owner", is_system_role=False
    )
    db_session.add(role)
    db_session.flush()
    db_session.add(models.UserRole(user_id=custom.id, role_id=role.id))
    db_session.flush()
    assert [
        user.id
        for user in sso_enforcement.owners(db_session, account_id=test_user.account_id)
    ] == [test_user.id]


def test_notice_deduplication_retry_and_delivery_state(db_session, test_user):
    from datetime import UTC, datetime
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    sso_enforcement.enqueue_owner_notices(
        db_session, user=test_user, recipients=[test_user, test_user]
    )
    pending = sso_enforcement.pending_notices(db_session)
    assert len(pending) == 1
    notice = pending[0]
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            models.SsoBreakGlassNotice(
                account_id=test_user.account_id,
                login_id=notice.login_id,
                actor_email=test_user.email,
                recipient_email=test_user.email,
            )
        )
        db_session.flush()
    sso_enforcement.finish_notice(db_session, notice=notice, delivered=False)
    assert notice.attempts == 1 and notice.delivered_at is None
    assert notice.retry_at > datetime.now(UTC)
    assert not sso_enforcement.pending_notices(db_session)
    notice.retry_at = datetime(2000, 1, 1, tzinfo=UTC)
    db_session.flush()
    assert sso_enforcement.pending_notices(db_session) == [notice]
    sso_enforcement.finish_notice(db_session, notice=notice, delivered=True)
    assert notice.attempts == 2 and notice.delivered_at is not None
    assert not sso_enforcement.pending_notices(db_session)
    assert len(list(db_session.scalars(select(models.SsoBreakGlassNotice)))) == 1


def test_notice_enqueue_rolls_back_with_callers_transaction(db_session, test_user):
    from sqlalchemy import select

    with pytest.raises(RuntimeError), db_session.begin_nested():
        sso_enforcement.enqueue_owner_notices(
            db_session, user=test_user, recipients=[test_user]
        )
        raise RuntimeError("synthetic transaction failure")
    assert db_session.scalar(select(models.SsoBreakGlassNotice.id)) is None


def test_selective_revocation_normalizes_stored_claim_and_preserves_sibling(
    db_session, test_user
):
    from uuid import uuid4
    from preloop.models.crud import crud_account, crud_user

    crud_person.update_profile(
        db_session,
        membership_id=test_user.id,
        changes={"email": "member@bücher.example"},
    )
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Sibling", "is_active": True}
    )
    sibling = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "person_id": test_user.person_id,
            "username": f"sibling-{uuid4().hex}",
            "email": test_user.email,
            "is_active": True,
        },
    )
    person = crud_person.resolve_for_membership(db_session, membership_id=test_user.id)
    generations = (
        person.auth_generation,
        test_user.auth_generation,
        sibling.auth_generation,
    )
    assert (
        sso_enforcement.revoke_non_sso(
            db_session, account_id=test_user.account_id, domains={"bücher.example"}
        )
        == 1
    )
    assert sibling.non_sso_auth_generation == 0
    assert (
        person.auth_generation,
        test_user.auth_generation,
        sibling.auth_generation,
    ) == generations


@pytest.mark.asyncio
@pytest.mark.parametrize("deny", [False, True])
async def test_opaque_exchange_gates_before_rows_and_preserves_context(
    db_session, test_user, monkeypatch, deny
):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from fastapi import HTTPException
    from sqlalchemy import select
    from preloop.api.endpoints.oauth_server import _issue_opaque_tokens
    from preloop.services import authentication_policy

    context = {
        "amr": ["password"],
        "auth_account_id": str(test_user.account_id),
        "nsg": 0,
    }
    code = SimpleNamespace(
        user_id=test_user.id,
        account_id=test_user.account_id,
        client_id="synthetic-mcp",
        auth_generation=test_user.auth_generation,
        auth_context=context,
        scopes=["read"],
        resource=None,
    )
    gate = Mock()
    if deny:
        gate.authorize.side_effect = HTTPException(403, "SSO required")
    monkeypatch.setattr(
        authentication_policy,
        "get_plugin_manager",
        lambda: Mock(get_service=Mock(return_value=gate)),
    )
    response = await _issue_opaque_tokens(db_session, code)
    payload = json.loads(response.body)
    access = list(db_session.scalars(select(models.OAuthMCPAccessToken)))
    refresh = list(db_session.scalars(select(models.OAuthMCPRefreshToken)))
    assert gate.authorize.call_args.kwargs["purpose"] == "exchange"
    if deny:
        assert payload["error"] == "invalid_grant"
        assert not access and not refresh
    else:
        assert response.status_code == 200
        assert payload["access_token"] and payload["refresh_token"]
        assert len(access) == len(refresh) == 1
        for row in [*access, *refresh]:
            assert row.auth_context == context
            assert row.auth_generation == test_user.auth_generation
